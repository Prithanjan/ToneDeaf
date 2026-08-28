"""
RuntimeStopper — the automated half of the cost-safety plane.

Invoked by an SNS message from AWS Budgets, or directly by hand as an emergency stop:

    aws lambda invoke --function-name sih26104-runtime-stopper /dev/stdout

**What it does, and why it is not one API call.** Stopping GPU spend requires *two* independent
actions, and doing only one of them looks like it worked:

  1. Set both ECS services' `desiredCount` to 0. Without this, ECS keeps trying to place tasks and
     the scheduler will pull capacity back in.
  2. Set the GPU Auto Scaling Group's min/max/desired to 0.

Step 2 is the one people get wrong. `ec2 stop-instances` on the `g4dn.xlarge` does not work — the ASG
notices an unhealthy/absent instance and **launches a replacement**, so the bill continues and the
operator has a terminal that reported success. `max` must go to 0 as well as `desired`, because a
non-zero `max` with managed scaling or any future scaling policy leaves the door open.

The order matters. Services first, then capacity: draining tasks before removing the instances they
run on avoids a window where ECS is frantically trying to place a task on capacity that is
disappearing. It is not harmful in the other order, just noisier in the logs.

**Everything here is idempotent.** Setting desiredCount to 0 on a service that is already 0 is a
successful no-op, so a duplicate SNS delivery — which SNS explicitly allows — cannot do damage. And a
partial failure is safe to retry: each action is independent.

**Failures are collected, not raised on the first one.** If `ecs:UpdateService` fails for the Gateway,
the Scorer and the ASG must still be stopped — the Scorer is the expensive one. The handler raises at
the *end* if anything failed, so Lambda's own retry and the error metric still fire.

Region is explicit. This function runs in us-east-1 (AWS Budgets can only publish to an SNS topic
there) while every runtime resource lives in ap-south-1. A boto3 client without an explicit region
would default to the function's own region and quietly find nothing to stop.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TARGET_REGION = os.environ["TARGET_REGION"]
CLUSTER_NAME = os.environ["CLUSTER_NAME"]
# Comma-separated. Kept as config rather than hardcoded so ComputeStack's service names stay the
# single source of truth.
SERVICE_NAMES = [s.strip() for s in os.environ["SERVICE_NAMES"].split(",") if s.strip()]
ASG_NAME = os.environ["ASG_NAME"]

# Short, bounded retries. This runs under a Lambda timeout and the failure mode we care about is a
# transient throttle, not a long outage — and if the API is genuinely down, failing fast so the error
# metric fires is more useful than burning the whole timeout.
_BOTO_CONFIG = Config(
    region_name=TARGET_REGION,
    retries={"max_attempts": 4, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)

ecs = boto3.client("ecs", config=_BOTO_CONFIG)
autoscaling = boto3.client("autoscaling", config=_BOTO_CONFIG)


def _stop_services() -> tuple[list[dict], list[str]]:
    """Set every configured ECS service to desiredCount 0."""
    done: list[dict] = []
    failures: list[str] = []

    for service in SERVICE_NAMES:
        try:
            response = ecs.update_service(
                cluster=CLUSTER_NAME,
                service=service,
                desiredCount=0,
            )
            previous = response["service"]["runningCount"]
            done.append({"service": service, "desiredCount": 0, "wasRunning": previous})
            logger.info(
                "service %s set to desiredCount=0 (was running %s)", service, previous
            )
        except ecs.exceptions.ServiceNotFoundException:
            # Not a failure. `deployRuntime=false` is the normal state and the services still exist,
            # but a torn-down ComputeStack has no services at all — and "nothing to stop" is the
            # outcome we wanted.
            logger.info("service %s not found — nothing to stop", service)
            done.append({"service": service, "state": "not_found"})
        except ecs.exceptions.ClusterNotFoundException:
            logger.info("cluster %s not found — nothing to stop", CLUSTER_NAME)
            done.append({"service": service, "state": "cluster_not_found"})
        except Exception as exc:  # noqa: BLE001 — deliberate: collect and continue
            # Broad on purpose. The Scorer must still be stopped even if the Gateway raises something
            # unanticipated, and the Scorer is where the money is.
            logger.exception("failed to stop service %s", service)
            failures.append(f"ecs:UpdateService {service}: {exc}")

    return done, failures


def _stop_capacity() -> tuple[dict, list[str]]:
    """Set the GPU ASG's min, max and desired capacity all to 0.

    `max=0` as well as `desired=0`. With a non-zero max, anything that later touches desired capacity
    — a scaling policy, ECS managed scaling, a console click — can bring the instance back. Zeroing
    max removes the permission rather than just the current value.
    """
    failures: list[str] = []
    result: dict = {"asg": ASG_NAME}

    try:
        described = autoscaling.describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME]
        )
        groups = described.get("AutoScalingGroups", [])
        if not groups:
            logger.info("ASG %s not found — nothing to stop", ASG_NAME)
            return {"asg": ASG_NAME, "state": "not_found"}, failures

        before = groups[0]
        result["before"] = {
            "min": before["MinSize"],
            "max": before["MaxSize"],
            "desired": before["DesiredCapacity"],
            "instances": len(before.get("Instances", [])),
        }

        autoscaling.update_auto_scaling_group(
            AutoScalingGroupName=ASG_NAME,
            MinSize=0,
            MaxSize=0,
            DesiredCapacity=0,
        )
        result["after"] = {"min": 0, "max": 0, "desired": 0}
        logger.info(
            "ASG %s set to min=0 max=0 desired=0 (was %s)", ASG_NAME, result["before"]
        )
    except Exception as exc:  # noqa: BLE001 — deliberate: report, do not mask
        logger.exception("failed to stop ASG %s", ASG_NAME)
        failures.append(f"autoscaling:UpdateAutoScalingGroup {ASG_NAME}: {exc}")

    return result, failures


def _describe_trigger(event: object) -> str:
    """Best-effort human description of what invoked this, for the log line."""
    if not isinstance(event, dict):
        return "direct invocation"
    records = event.get("Records")
    if isinstance(records, list) and records:
        sns = records[0].get("Sns", {}) if isinstance(records[0], dict) else {}
        subject = sns.get("Subject") or "no subject"
        # The message body is a Budgets JSON payload. It is logged as-is because it names the budget
        # and the threshold that fired, which is the one thing an operator wants from this log line.
        return f"SNS: {subject}"
    return "direct invocation"


def handler(event, context):  # noqa: ANN001, ANN201 — Lambda signature
    """Stop all runtime spend. Safe to call at any time, including when nothing is running."""
    trigger = _describe_trigger(event)
    logger.warning(
        "RuntimeStopper invoked (%s) — zeroing runtime in %s", trigger, TARGET_REGION
    )

    services, service_failures = _stop_services()
    capacity, capacity_failures = _stop_capacity()
    failures = service_failures + capacity_failures

    result = {
        "region": TARGET_REGION,
        "cluster": CLUSTER_NAME,
        "trigger": trigger,
        "services": services,
        "capacity": capacity,
        "failures": failures,
    }
    logger.info("RuntimeStopper result: %s", json.dumps(result, default=str))

    if failures:
        # Raise *after* doing everything possible. This surfaces on the Errors metric and triggers
        # Lambda's own async retry, and by this point every independent action has been attempted.
        raise RuntimeError(
            f"RuntimeStopper completed with {len(failures)} failure(s): {'; '.join(failures)}. "
            "Runtime may still be billing — verify by hand (aws-setup-instructions.md §11)."
        )

    return result

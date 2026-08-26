#!/usr/bin/env python3
"""Render the annotated IAM policy sources in ``infra/iam/`` into deployable JSON.

WHY THIS EXISTS
===============
The files in ``infra/iam/`` are *annotated source*, not deployable documents. They carry ``"//"``
keys holding the reasoning for every statement — which is the point of them, because an IAM policy
with no recorded rationale is a policy nobody dares narrow later.

But **IAM rejects those keys.** The policy grammar admits exactly ``Version``, ``Id`` and
``Statement`` at the top level, and ``Sid``, ``Effect``, ``Principal``, ``NotPrincipal``, ``Action``,
``NotAction``, ``Resource``, ``NotResource`` and ``Condition`` inside a statement. Anything else is
a hard failure::

    An error occurred (MalformedPolicyDocument) when calling the CreateRole operation:
    Syntax errors in policy.

That error names no key and no line. Passing ``file://infra/iam/gh-actions-trust-policy.json``
straight to the CLI therefore fails in a way that looks like a corrupt file rather than a comment
this project put there on purpose.

The obvious fix is ``jq 'del(.["//"])'``. It is not portable enough to be the documented step: ``jq``
is absent from a stock Windows/Git-Bash setup, which is exactly where Phase-0 setup gets done by
hand. Python 3.12 is already a hard dependency of the Gateway and Scorer, so this adds nothing new.

WHAT IT ALSO DOES, AND WHY IT IS THE SAME SCRIPT
================================================
Both files carry placeholders (``<AWS_ACCOUNT_ID>``, ``<GITHUB_OWNER>``, ``<GITHUB_REPO>``) that had
to be substituted by hand anyway. Folding that in removes a whole class of failure rather than
documenting it: the strip and the substitution are now one command that either produces a correct
pair of documents or refuses.

**It refuses on any leftover placeholder.** That check is the reason this is worth a script at all.
An unsubstituted ``<AWS_ACCOUNT_ID>`` in the *trust* policy is not a loud failure — the ARN simply
matches nothing, the role deploys, and every deploy fails later with an OIDC error that points at
GitHub. Failing here, with the placeholder named, is the difference between a five-second fix and an
afternoon.

USAGE
=====
    python scripts/render_iam_policies.py --account-id 123456789012 \\
        --github-owner myorg --github-repo sih26104

Writes ``infra/iam/rendered/*.json``. That directory is self-ignoring (it ships its own
``.gitignore`` containing ``*``) because the rendered files contain a real account id, and R-34's
spirit is that nothing account-specific lands in Git. The ignore is local to the directory rather
than a line in the root ``.gitignore`` so that it holds regardless of what that file does.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── The IAM policy grammar, as a whitelist ───────────────────────────────────────────────────────
#
# Checked rather than assumed. Stripping only `"//"` would leave a typo'd key (`"Resources"`,
# `"Effects"`) to be discovered by AWS with the same unhelpful MalformedPolicyDocument, and a typo in
# a Deny statement's key is the dangerous case: `"NotActions"` instead of `"NotAction"` yields a
# statement that denies far more than intended, or nothing at all.
TOP_LEVEL_KEYS = {"Version", "Id", "Statement"}
STATEMENT_KEYS = {
    "Sid",
    "Effect",
    "Principal",
    "NotPrincipal",
    "Action",
    "NotAction",
    "Resource",
    "NotResource",
    "Condition",
}

COMMENT_KEY = "//"
PLACEHOLDER_RE = re.compile(r"<[A-Z_]+>")

# AWS caps an INLINE role policy at 10,240 characters (a managed policy is 6,144, which is smaller —
# this project uses inline, see aws-setup-instructions.md §4.2). Whitespace counts.
#
# Checked here because the limit is only hit at `aws iam put-role-policy` time, with
# `LimitExceeded: Maximum policy size of 10240 bytes exceeded`, at the end of a setup sequence — and
# the fix at that point (start deleting statements) is the worst moment to be choosing which
# permission to drop. The warning threshold exists so the ceiling is visible while there is still
# room to reorganise into a second attached policy.
INLINE_POLICY_LIMIT = 10_240
INLINE_POLICY_WARN_AT = 0.80

REPO_ROOT = Path(__file__).resolve().parent.parent
IAM_DIR = REPO_ROOT / "infra" / "iam"
OUT_DIR = IAM_DIR / "rendered"


def strip_comments(node: object) -> object:
    """Remove every ``"//"`` key, at any depth.

    Recursive rather than two targeted deletions: a ``Condition`` block may grow an annotation
    later, and a renderer that silently passes it through to AWS would reintroduce exactly the
    failure this script exists to prevent.
    """
    if isinstance(node, dict):
        return {k: strip_comments(v) for k, v in node.items() if k != COMMENT_KEY}
    if isinstance(node, list):
        return [strip_comments(v) for v in node]
    return node


def validate_grammar(doc: dict, name: str) -> list[str]:
    """Return a list of grammar violations. Empty means the document is shaped like a policy."""
    problems: list[str] = []

    unknown_top = set(doc) - TOP_LEVEL_KEYS
    if unknown_top:
        problems.append(f"{name}: unknown top-level key(s): {sorted(unknown_top)}")

    if doc.get("Version") != "2012-10-17":
        # The only other legal value is the 2008 version, which does not support policy variables or
        # most condition operators. If it ever appears here it is a copy-paste from an old example.
        problems.append(f"{name}: Version must be '2012-10-17', found {doc.get('Version')!r}")

    statements = doc.get("Statement")
    if not isinstance(statements, list) or not statements:
        problems.append(f"{name}: Statement must be a non-empty list")
        return problems

    for i, stmt in enumerate(statements):
        where = f"{name}: Statement[{i}]" + (f" ({stmt['Sid']})" if isinstance(stmt, dict) and "Sid" in stmt else "")
        if not isinstance(stmt, dict):
            problems.append(f"{where}: not an object")
            continue

        unknown = set(stmt) - STATEMENT_KEYS
        if unknown:
            problems.append(f"{where}: unknown key(s): {sorted(unknown)}")
        if stmt.get("Effect") not in {"Allow", "Deny"}:
            problems.append(f"{where}: Effect must be 'Allow' or 'Deny', found {stmt.get('Effect')!r}")
        if not ({"Action", "NotAction"} & set(stmt)):
            problems.append(f"{where}: needs Action or NotAction")

        # A resource policy (one with Principal) is the exception: a trust policy has no Resource,
        # because the resource IS the role the policy is attached to. An identity policy without
        # Resource is a malformed document.
        if "Principal" not in stmt and not ({"Resource", "NotResource"} & set(stmt)):
            problems.append(f"{where}: identity-policy statement needs Resource or NotResource")

    return problems


def substitute(text: str, mapping: dict[str, str]) -> str:
    for placeholder, value in mapping.items():
        text = text.replace(placeholder, value)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render infra/iam/*.json into deployable IAM policy documents.",
    )
    parser.add_argument(
        "--account-id",
        required=True,
        help="12-digit AWS account id. Substituted for <AWS_ACCOUNT_ID>.",
    )
    parser.add_argument("--github-owner", required=True, help="Substituted for <GITHUB_OWNER>.")
    parser.add_argument("--github-repo", required=True, help="Substituted for <GITHUB_REPO>.")
    args = parser.parse_args()

    # Checked because the failure is otherwise silent and late: a mistyped account id produces
    # syntactically valid ARNs that match nothing, and the trust policy then rejects every deploy
    # with an error that points at GitHub's OIDC rather than at this argument.
    if not re.fullmatch(r"\d{12}", args.account_id):
        print(
            f"ERROR: --account-id must be exactly 12 digits, got {args.account_id!r}.\n"
            "       Find it with: aws sts get-caller-identity --query Account --output text",
            file=sys.stderr,
        )
        return 2

    mapping = {
        "<AWS_ACCOUNT_ID>": args.account_id,
        "<GITHUB_OWNER>": args.github_owner,
        "<GITHUB_REPO>": args.github_repo,
    }

    sources = sorted(p for p in IAM_DIR.glob("*.json") if p.parent == IAM_DIR)
    if not sources:
        print(f"ERROR: no policy sources found in {IAM_DIR}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Self-ignoring output directory. Written every run so that a fresh clone plus one render leaves
    # no way to commit an account id, without depending on the root .gitignore.
    (OUT_DIR / ".gitignore").write_text(
        "# Rendered IAM policies contain a real AWS account id. Never commit them.\n"
        "# Regenerate with: python scripts/render_iam_policies.py --help\n"
        "*\n",
        encoding="utf-8",
    )

    failures: list[str] = []
    warnings: list[str] = []
    written: list[Path] = []

    for src in sources:
        raw = src.read_text(encoding="utf-8")
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            failures.append(f"{src.name}: not valid JSON: {exc}")
            continue

        stripped = strip_comments(doc)
        assert isinstance(stripped, dict)

        # Substitute on the SERIALISED form, after stripping. Placeholders appear inside ARN strings
        # at several depths, and one text pass over the comment-free document is both simpler than
        # walking the tree and guaranteed not to touch prose that has since been removed.
        rendered_text = substitute(json.dumps(stripped, indent=2), mapping)

        leftover = sorted(set(PLACEHOLDER_RE.findall(rendered_text)))
        if leftover:
            failures.append(
                f"{src.name}: unsubstituted placeholder(s) {leftover} — this script has no value "
                f"for them. Add a flag, or fix the source."
            )
            continue

        rendered = json.loads(rendered_text)
        problems = validate_grammar(rendered, src.name)
        if problems:
            failures.extend(problems)
            continue

        # Size gate. Applies only to the identity policy: a trust policy has its own, larger budget
        # and is one statement long, so measuring it would produce a warning nobody can act on.
        #
        # MEASURE THE BYTES THAT GET WRITTEN, NOT THE STRING. Two things make those differ, and both
        # push the number in the unsafe direction:
        #
        #   1. `Path.write_text()` in text mode translates "\n" to os.linesep, so on Windows every
        #      line gains a byte. This document is ~170 lines, so the file on disk was 5493 bytes
        #      while `len(rendered_text)` reported 5325 — a 168-byte gap that GROWS with the policy.
        #      Worse, it is platform-dependent: a policy that fits on the Linux CI runner could be
        #      rejected by AWS when rendered on the Windows workstation it is actually deployed from.
        #   2. Non-ASCII characters (the source comments use — and ⚠) encode to more than one byte.
        #      They are stripped with the comments here, but a Sid or a resource name could carry one.
        #
        # And AWS counts every one of those bytes: whitespace is permitted in a policy document for
        # readability but is NOT exempt from the size limit. So `newline="\n"` below pins the output
        # to one byte per line on every platform, and the gate measures the encoded result.
        is_trust_policy = any("Principal" in s for s in rendered["Statement"])
        payload = rendered_text + "\n"
        size = len(payload.encode("utf-8"))
        if not is_trust_policy:
            if size > INLINE_POLICY_LIMIT:
                failures.append(
                    f"{src.name}: {size} bytes exceeds the {INLINE_POLICY_LIMIT}-byte inline policy "
                    f"limit. Split it into a second policy attached to the same role rather than "
                    f"widening statements to save characters."
                )
                continue
            if size > INLINE_POLICY_LIMIT * INLINE_POLICY_WARN_AT:
                warnings.append(
                    f"{src.name}: {size}/{INLINE_POLICY_LIMIT} bytes "
                    f"({size / INLINE_POLICY_LIMIT:.0%}) — approaching the inline policy limit."
                )

        out = OUT_DIR / src.name
        out.write_text(payload, encoding="utf-8", newline="\n")
        written.append(out)

    for f in failures:
        print(f"ERROR: {f}", file=sys.stderr)
    if failures:
        print("\nNothing was written. Fix the above and re-run.", file=sys.stderr)
        return 1

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    print(f"Rendered {len(written)} policy document(s) into {OUT_DIR.relative_to(REPO_ROOT)}/:")
    for out in written:
        size = out.stat().st_size
        print(f"  {out.name}  ({size} bytes)")
    print(
        # ROLE NAME: `gh-actions-deploy-role`, and it is not a free choice. It appears in 18 places —
        # every `Assume … via OIDC` step across five workflows, the error message that tells you how to
        # set AWS_DEPLOY_ROLE_ARN, architecture.md's trust diagram, and secret-scan.yml's detector
        # fixtures. An earlier draft of this banner said `sih26104-gh-actions-deploy` and would have had
        # the operator create a role nothing else in the repo refers to; the resulting failure is an OIDC
        # error at deploy time pointing at GitHub. Section reference is 3.3, not 4.2.
        "\nNext (aws-setup-instructions.md §3.2 and §3.3):\n"
        "  aws iam create-role --role-name gh-actions-deploy-role \\\n"
        f"      --assume-role-policy-document file://{(OUT_DIR / 'gh-actions-trust-policy.json').relative_to(REPO_ROOT)} \\\n"
        '      --description "SIH26104 CI deploy role - OIDC only, no keys"\n'
        "  aws iam put-role-policy --role-name gh-actions-deploy-role \\\n"
        "      --policy-name sih26104-deploy \\\n"
        f"      --policy-document file://{(OUT_DIR / 'gh-actions-deploy-policy.json').relative_to(REPO_ROOT)}\n"
        "\n  Then: gh variable set AWS_DEPLOY_ROLE_ARN "
        "--body arn:aws:iam::<ACCOUNT_ID>:role/gh-actions-deploy-role"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

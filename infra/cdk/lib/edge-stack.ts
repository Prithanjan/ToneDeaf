/**
 * EdgeStack — 5 of 5. **Deploy last.**
 *
 * The public edge, and nothing else: a private S3 bucket for the PWA behind an Origin Access Control,
 * one CloudFront distribution that is the only publicly reachable thing in the account, and the
 * security-group rule that lets it reach the Gateway.
 *
 * **The ALB is not here.** It is created in `ComputeStack` and passed in. An ECS service with a
 * load-balancer configuration gets a hard dependency on the ALB's listener, so an ALB in this stack
 * closes a cycle with the Gateway service — the reasoning is recorded at `ComputeStack.gatewayAlb`.
 * CloudFront VPC origins are what make that split harmless: the ALB stays private, with no public
 * subnet and no public DNS, and CloudFront reaches it from inside the VPC.
 *
 * **Two behaviours, not one.** `/ws/v1/stream` and the API cannot share a cache policy with the static
 * bundle. A cached WebSocket upgrade is not slow, it is broken; a cached `POST /v1/sessions` would
 * serve one caller's ticket to another. So the default behaviour is the static bundle and every
 * dynamic path is an explicit, uncached, all-headers-forwarded exception.
 *
 * ⚠️ This stack contains the one late-bound piece of the network graph: the ALB's ingress rule cannot
 * be written until the distribution exists, because CloudFront's service-managed security group for a
 * VPC origin does not exist — and its id is not returned by CloudFormation — until then. It is resolved
 * here with a lookup custom resource rather than left as a manual step (aws-setup-instructions.md §9.3).
 */
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

export interface EdgeStackProps extends cdk.StackProps {
  /** Needed only to scope the security-group lookup to this VPC. */
  readonly vpc: ec2.IVpc;
  readonly albSecurityGroup: ec2.ISecurityGroup;
  /** Created in `ComputeStack`. See `ComputeStack.gatewayAlb`. */
  readonly gatewayAlb: elbv2.IApplicationLoadBalancer;
}

/** The one WSS path. Named once: it is a cache behaviour here and a `connect-src` in the CSP below. */
const WS_PATH = '/ws/v1/stream';

export class EdgeStack extends cdk.Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly siteBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: EdgeStackProps) {
    super(scope, id, props);

    // ── Static origin: private bucket, no website hosting ─────────────────────────────────────────

    /**
     * `blockPublicAccess: BLOCK_ALL` and no static-website configuration. S3 website endpoints only
     * speak HTTP and require a public bucket; an OAC-fronted private bucket gives TLS, keeps the
     * bucket unreachable except through the distribution, and means a leaked bucket name is not a
     * leaked bucket.
     */
    this.siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: false,
      // DESTROY + autoDelete so teardown is one command and cannot leave a billing tail. The bundle is
      // a build artifact; it is reproducible from the commit, so there is nothing here worth keeping.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ── CloudFront ────────────────────────────────────────────────────────────────────────────────

    /**
     * The VPC origin. A static factory, not a constructor — `origins.VpcOrigin` is abstract with a
     * protected constructor, so `new origins.VpcOrigin(...)` does not compile.
     *
     * CloudFront creates and manages a security group for this, which is why the ALB's ingress rule is
     * resolved at the bottom of this file rather than in `NetworkStack`.
     *
     * `domainName` is deliberately omitted: for a VPC origin it defaults to the endpoint's own domain
     * name, and passing `alb.loadBalancerDnsName` would be a second, redundant source for the same
     * value.
     */
    const vpcOrigin = origins.VpcOrigin.withApplicationLoadBalancer(props.gatewayAlb, {
      /**
       * **Not cosmetic — this is the difference between a working origin and a 502.**
       *
       * The default is `MATCH_VIEWER`. The viewer here is always HTTPS (`REDIRECT_TO_HTTPS` below), so
       * `MATCH_VIEWER` would make CloudFront dial the ALB on **443**, where there is no listener at
       * all. `HTTP_ONLY` is what matches the single HTTP:80 listener above.
       */
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80,
      /**
       * 60 s, which is the ceiling. Both of these are capped at 60 unless a CloudFront *origin response
       * timeout* quota increase has been approved for the account — above that, the value is rejected
       * at deploy time rather than clamped, so 61 would fail the stack. The hard limit even with the
       * increase is 180 s.
       *
       * These govern the HTTP request/response phase. Once a connection is upgraded to a WebSocket it
       * is a tunnel, and the lifetime that actually bounds a stream is the ALB's `idleTimeout` above.
       */
      readTimeout: cdk.Duration.seconds(60),
      keepaliveTimeout: cdk.Duration.seconds(60),
    });

    /**
     * One cache policy for every dynamic path: cache nothing, forward everything.
     *
     * `CACHING_DISABLED` is not a performance compromise to revisit — for a WebSocket upgrade and for
     * ticket issuance, caching is a correctness failure. A cached `POST /v1/sessions` response would
     * hand one caller's single-use stream ticket to the next caller.
     */
    const dynamicBehaviourBase = {
      origin: vpcOrigin,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      /**
       * `ALL_VIEWER` forwards every header, and the WebSocket handshake does not work without it.
       * `Sec-WebSocket-Protocol` carries the stream ticket — dropping or rewriting it turns every
       * connection into `AUTH_TICKET_MISSING`, which reads like an auth bug and is really a proxy
       * configuration bug. `Origin` matters too: the Gateway checks it against an exact allow-list, so
       * a stripped `Origin` fails closed and a rewritten one fails confusingly.
       */
      originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
    };

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'SIH26104 Voice Integrity Control Plane — the only public entry point',

      /**
       * Default behaviour is the *static bundle*. Everything dynamic is an explicit exception below,
       * which is the safe default direction: a new API path that nobody added a behaviour for gets
       * served from S3 and returns a visible 404, rather than being cached and served to the wrong
       * caller.
       */
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
        compress: true,
        responseHeadersPolicy: this.securityHeaders(),
      },

      additionalBehaviors: {
        /**
         * The WSS stream. CloudFront supports WebSocket on a behaviour with caching disabled and all
         * viewer headers forwarded; there is no separate "enable websockets" switch, which is exactly
         * why a misconfigured cache policy here presents as a mysterious handshake failure.
         */
        [WS_PATH]: dynamicBehaviourBase,
        // Session creation, ticket issuance, decision reads.
        '/v1/*': dynamicBehaviourBase,
        // Liveness and readiness, proxied so an operator can reach them without VPC access.
        '/healthz': dynamicBehaviourBase,
        '/readyz': dynamicBehaviourBase,
        '/metrics': dynamicBehaviourBase,
      },

      defaultRootObject: 'index.html',

      /**
       * SPA history fallback. A deep link like `/session/abc` is not a file in the bucket, and S3
       * answers 403 (not 404) for a missing key behind OAC — so both are rewritten to `index.html`
       * with a 200.
       *
       * This is safe only *because* every dynamic path above is an explicit behaviour. If the API were
       * served by the default behaviour, this rule would turn a genuine API 404 into an HTML page and
       * a client would parse the SPA shell as JSON.
       */
      errorResponses: [
        { httpStatus: 403, responseHttpStatus: 200, responsePagePath: '/index.html', ttl: cdk.Duration.seconds(0) },
        { httpStatus: 404, responseHttpStatus: 200, responsePagePath: '/index.html', ttl: cdk.Duration.seconds(0) },
      ],

      /**
       * ⚠️ **TLS 1.2 cannot be enforced on this distribution, and it is honest to say so.**
       *
       * `minimumProtocolVersion` was set here and removed: CDK rejects it with *"Ignoring
       * 'minimumProtocolVersion': it has no effect without a custom 'certificate'. The distribution
       * uses the CloudFront default certificate, whose security policy is fixed at TLSv1."* Leaving the
       * property in place would have documented a guarantee the deployed system does not make.
       *
       * The default `*.cloudfront.net` certificate is what makes the demo reachable over HTTPS with no
       * DNS work at all, and that trade is worth taking for a demo. Raising the floor to TLS 1.2
       * requires a custom domain plus an ACM certificate **in us-east-1** — Phase 4, tracked as such.
       */

      /**
       * `PRICE_CLASS_100` — North America and Europe edges only. The demo audience is in India, so
       * this looks wrong and is not: the origin is `ap-south-1`, and requests from India reach it
       * either way. Price class 100 is the cheapest option and the latency difference to a demo that
       * is not latency-bound at the edge is immaterial. Raise it if a judge is on another continent.
       */
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,

      httpVersion: cloudfront.HttpVersion.HTTP2,
      enableIpv6: true,
      // Off. Access logs need a second bucket, bill per GB, and record request paths — and a request
      // path here can carry a session identifier.
      enableLogging: false,
    });

    /**
     * **Closing the network graph.**
     *
     * CloudFront provisions a security group named `CloudFront-VPCOrigins-Service-SG` in the VPC when
     * the first VPC origin is created, and **CloudFormation does not return its id** — which is why
     * `NetworkStack` deliberately leaves the ALB's ingress empty and this stack finishes the job.
     *
     * Two ways to finish it, and this is the tighter one:
     *
     *   - The documented "simplest approach" is to allow the `com.amazonaws.global.cloudfront
     *     .origin-facing` prefix list. That works, but it admits **every** CloudFront distribution in
     *     every AWS account, so "the ALB's only ingress is our CloudFront" would stop being true.
     *   - This lookup names the exact service-managed group for *this* VPC. The invariant survives.
     *
     * The cost is a lookup custom resource: a small Lambda that calls `ec2:DescribeSecurityGroups`
     * once. It is worth it — the alternative left a manual `authorize-security-group-ingress` step
     * whose failure mode is a 502 that reads like a broken Gateway, and manual steps are the ones that
     * get skipped.
     *
     * `installLatestAwsSdk: false` uses the SDK bundled in the Lambda runtime. The default fetches the
     * latest SDK at deploy time, which needs egress, makes deploys non-reproducible, and is the usual
     * cause of a custom resource that worked last month.
     */
    const cfOriginSgLookup = new cr.AwsCustomResource(this, 'CloudFrontVpcOriginSgLookup', {
      onUpdate: {
        service: 'EC2',
        action: 'DescribeSecurityGroups',
        parameters: {
          Filters: [
            { Name: 'group-name', Values: ['CloudFront-VPCOrigins-Service-SG'] },
            { Name: 'vpc-id', Values: [props.vpc.vpcId] },
          ],
        },
        // A fixed physical id: this resource reads state and owns nothing, so it must not be replaced
        // on every deploy.
        physicalResourceId: cr.PhysicalResourceId.of('CloudFrontVpcOriginSgLookup'),
      },
      // `DescribeSecurityGroups` does not support resource-level permissions, so ANY_RESOURCE is the
      // only expressible scope. It is a read-only call.
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
        resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE,
      }),
      installLatestAwsSdk: false,
      timeout: cdk.Duration.seconds(60),
    });
    // Explicit: the group does not exist until the distribution (and its VPC origin) does.
    cfOriginSgLookup.node.addDependency(this.distribution);

    /**
     * `CfnSecurityGroupIngress` in **this** stack, not `albSecurityGroup.addIngressRule(...)`.
     *
     * `addIngressRule` attaches the rule to the security group's *own* stack — `NetworkStack` — and the
     * source id here is a token produced by `EdgeStack`. That would make NetworkStack depend on
     * EdgeStack while EdgeStack already depends on NetworkStack for the VPC: a cycle, and `cdk synth`
     * fails with a message that does not obviously point back to this line.
     *
     * Declaring the L1 here keeps both references pointing the right way: the group id comes *from*
     * NetworkStack, the source id is local.
     */
    new ec2.CfnSecurityGroupIngress(this, 'AlbIngressFromCloudFront', {
      groupId: props.albSecurityGroup.securityGroupId,
      ipProtocol: 'tcp',
      fromPort: 80,
      toPort: 80,
      sourceSecurityGroupId: cfOriginSgLookup.getResponseField('SecurityGroups.0.GroupId'),
      description: 'CloudFront VPC origin → Gateway ALB (resolved at deploy time)',
    });

    new cdk.CfnOutput(this, 'ManualAlbIngressFallback', {
      description:
        'ONLY if CloudFrontVpcOriginSgLookup fails: find the SG in the console and run this by hand (§9.3)',
      value: cdk.Fn.join(' ', [
        'aws ec2 authorize-security-group-ingress --group-id',
        props.albSecurityGroup.securityGroupId,
        '--protocol tcp --port 80 --source-group <CLOUDFRONT_VPCORIGINS_SERVICE_SG_ID>',
      ]),
    });

    new cdk.CfnOutput(this, 'DistributionDomainName', {
      value: this.distribution.distributionDomainName,
      description:
        'Set allowedOrigins in cdk.json to https://<this> and redeploy ComputeStack (the second pass)',
    });
    new cdk.CfnOutput(this, 'AllowedOriginsValue', {
      value: `https://${this.distribution.distributionDomainName}`,
      description: 'Paste this verbatim into cdk.json allowedOrigins — exact origin, no trailing slash',
    });
    new cdk.CfnOutput(this, 'SiteBucketName', {
      value: this.siteBucket.bucketName,
      description: 'aws s3 sync pwa/dist s3://<this>/ --delete',
    });
    new cdk.CfnOutput(this, 'AlbDnsName', { value: props.gatewayAlb.loadBalancerDnsName });
  }

  /**
   * Response headers for the static bundle.
   *
   * The CSP mirrors the Caddyfile's, because a policy that differs between the local tier and AWS is
   * a policy that gets debugged twice. `connect-src` must name the wss origin explicitly — `'self'`
   * does **not** cover a `wss:` scheme in every browser, and the failure is a WebSocket blocked by
   * CSP with a console message that does not obviously point at this header.
   *
   * `microphone=(self)` in `Permissions-Policy` is the only capability granted. `camera`, `geolocation`
   * and `payment` are explicitly empty rather than omitted: an omitted directive inherits, an empty one
   * denies.
   */
  private securityHeaders(): cloudfront.ResponseHeadersPolicy {
    return new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      comment: 'SIH26104 — mirrors infra/compose/Caddyfile',
      securityHeadersBehavior: {
        contentSecurityPolicy: {
          override: true,
          contentSecurityPolicy: [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            // The wss origin is the distribution itself. Written as a wildcard on the CloudFront
            // domain because the exact hostname is not knowable while this policy is being built —
            // the distribution that will carry it is still being constructed.
            "connect-src 'self' wss://*.cloudfront.net",
            // blob: is required: captured audio is handed to the worklet as a blob URL and never
            // uploaded anywhere but the WSS stream (rules.md R-14).
            "media-src 'self' blob:",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            'upgrade-insecure-requests',
          ].join('; '),
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.NO_REFERRER,
          override: true,
        },
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.days(365),
          includeSubdomains: true,
          override: true,
        },
      },
      customHeadersBehavior: {
        customHeaders: [
          {
            header: 'Permissions-Policy',
            value: 'microphone=(self), camera=(), geolocation=(), payment=()',
            override: true,
          },
        ],
      },
    });
  }
}

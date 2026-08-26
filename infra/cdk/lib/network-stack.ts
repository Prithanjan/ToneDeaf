/**
 * NetworkStack — 1 of 5.
 *
 * VPC, subnets, exactly one NAT gateway, and the security groups that make the network posture
 * deny-by-default. The security groups are created *here* rather than next to the services they
 * protect, and that is the substance of this file: the ingress rules between them encode
 * architecture.md §4.2 as a graph, so the chain
 *
 *     CloudFront → ALB :443 → Gateway :8080 → Scorer :50051
 *                                           → RDS :5432
 *
 * is readable in one place instead of being assembled from four stacks. A rule added in the wrong
 * stack is how a Scorer ends up reachable from the ALB.
 */
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly gatewaySecurityGroup: ec2.SecurityGroup;
  public readonly scorerSecurityGroup: ec2.SecurityGroup;
  public readonly databaseSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      ipAddresses: ec2.IpAddresses.cidr('10.42.0.0/16'),
      /**
       * Two AZs, not three. RDS is single-AZ for this demo, so a third AZ buys no availability — it
       * buys a third set of subnets and route tables to reason about. Two is also the minimum an ALB
       * accepts.
       *
       * ⚠️ Check `g4dn.xlarge` availability in the AZs you actually get. It is not offered in every
       * AZ of every region, and the failure mode is an ASG that can never launch an instance while
       * reporting no error until it tries (aws-setup-instructions.md §1, H-2).
       */
      maxAzs: 2,
      /**
       * **One** NAT gateway, shared. This is the single largest idle line item in the account — one
       * NAT is roughly $0.05/hour whether or not anything flows through it, so two would double the
       * only cost that accrues while the runtime is at desired 0.
       *
       * The trade is real and accepted: if that AZ's NAT fails, private egress in the other AZ stops.
       * For a five-day demo with desired-count 1, that is not the risk worth paying to remove.
       */
      natGateways: 1,
      subnetConfiguration: [
        {
          // ALB and NAT live here. Nothing else — no task ever gets a public IP.
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          // Gateway and Scorer tasks. Egress via NAT for ECR pulls and Secrets Manager.
          name: 'app-private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
          cidrMask: 22,
        },
        {
          /**
           * RDS. `PRIVATE_ISOLATED`, so there is no route to the NAT at all — the database cannot
           * reach the internet even if a security group were misconfigured. Two independent controls
           * (routing and SG) rather than one.
           */
          name: 'data-private',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
      // Off. Useful for forensics, and it bills per GB ingested — which is exactly the kind of small
      // continuous cost that survives a teardown nobody checked.
      flowLogs: {},
    });

    /**
     * Gateway interface endpoints for Secrets Manager and CloudWatch Logs.
     *
     * Cost-neutral in the wrong direction — interface endpoints bill hourly, like the NAT. They are
     * here anyway for one reason: **secrets must not traverse the NAT**. Fetching a task's secrets
     * over public AWS endpoints via NAT works and is normal, but it puts the ticket signing key and
     * the audit chain key on a path with an internet gateway at the end of it, and a private path is
     * cheap to justify to a security reviewer where "it's TLS anyway" is not.
     */
    this.vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      privateDnsEnabled: true,
    });

    // ── Security groups: created together so the whole chain is reviewable here ───────────────────

    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc: this.vpc,
      description: 'Internal ALB. Ingress ONLY from the CloudFront service-managed prefix list.',
      allowAllOutbound: false,
    });

    this.gatewaySecurityGroup = new ec2.SecurityGroup(this, 'GatewaySg', {
      vpc: this.vpc,
      description: 'Gateway tasks. Ingress only from the ALB SG.',
      // Needs egress: ECR, Secrets Manager, CloudWatch, and the JWKS endpoint of the identity
      // provider. Narrowing this to a prefix-list set is Phase 4 work, not a five-day-window control.
      allowAllOutbound: true,
    });

    this.scorerSecurityGroup = new ec2.SecurityGroup(this, 'ScorerSg', {
      vpc: this.vpc,
      description: 'Scorer tasks. Ingress only from the Gateway SG on 50051.',
      allowAllOutbound: true,
    });

    this.databaseSecurityGroup = new ec2.SecurityGroup(this, 'DatabaseSg', {
      vpc: this.vpc,
      description: 'RDS. Ingress only from the Gateway SG on 5432. No egress.',
      allowAllOutbound: false,
    });

    /**
     * ALB → Gateway. The ALB's *own* ingress is not set here: CloudFront provisions a
     * service-managed security group for a VPC origin, and its id neither exists nor is returned by
     * CloudFormation until the distribution does. That rule is added in `EdgeStack`, where a lookup
     * custom resource resolves the group by name and the ingress is declared as an L1 in that stack —
     * declaring it against this object would point NetworkStack at EdgeStack and close a cycle
     * (aws-setup-instructions.md §9.3).
     */
    this.gatewaySecurityGroup.addIngressRule(
      this.albSecurityGroup,
      ec2.Port.tcp(8080),
      'ALB to Gateway HTTP/WS',
    );
    this.albSecurityGroup.addEgressRule(
      this.gatewaySecurityGroup,
      ec2.Port.tcp(8080),
      'ALB to Gateway HTTP/WS',
    );

    /**
     * Gateway → Scorer. Source is the Gateway SG, never a CIDR.
     *
     * This rule is the network half of the detection/decision separation. The Scorer is reachable
     * from exactly one place, so the claim "the Scorer never sees a purpose_code or session history"
     * (enforced structurally by the gRPC message shape) is backed by there being no other caller
     * that could send one.
     */
    this.scorerSecurityGroup.addIngressRule(
      this.gatewaySecurityGroup,
      ec2.Port.tcp(50051),
      'Gateway to Scorer gRPC',
    );

    /**
     * Gateway → RDS. Nothing else may reach the database — not the Scorer, not the ALB.
     *
     * The port is the literal `5432` rather than the DB instance's own endpoint port, and it has to be:
     * reading the port off the instance would make this stack reference `DataStack`, which references
     * this one. `data-stack.ts` pins the same 5432, and `compute-stack.ts` records the cycle in full.
     */
    this.databaseSecurityGroup.addIngressRule(
      this.gatewaySecurityGroup,
      ec2.Port.tcp(5432),
      'Gateway to PostgreSQL',
    );

    new cdk.CfnOutput(this, 'VpcId', { value: this.vpc.vpcId, exportName: 'Sih26104VpcId' });
    new cdk.CfnOutput(this, 'AlbSgId', {
      value: this.albSecurityGroup.securityGroupId,
      description: 'Needed for the manual CloudFront VPC-origin SG step (aws-setup §9.3)',
    });
  }
}

"""
Networking Stack — VPC, subnets, and security groups.

Creates the foundational network infrastructure that other stacks
(Lambda, ECS) deploy into.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct


class NetworkingStack(cdk.Stack):
    """VPC with public and private subnets for the DevOps Agent."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(scope, construct_id, **kwargs)

        # ── VPC ──────────────────────────────────────────────────────
        self.vpc = ec2.Vpc(
            self,
            "AgentVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # ── Security Group for Lambda / ECS ──────────────────────────
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentSG",
            vpc=self.vpc,
            description="Security group for DevOps Agent compute",
            allow_all_outbound=True,
        )

        # ── Outputs ──────────────────────────────────────────────────
        cdk.CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        cdk.CfnOutput(self, "SecurityGroupId", value=self.agent_sg.security_group_id)

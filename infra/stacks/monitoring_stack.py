"""
Monitoring Stack — CloudWatch alarms and EventBridge rules.

**No instance IDs need to be specified anywhere.**

At synth / deploy time this stack calls boto3 to discover every EC2
instance currently in the account and provisions CPU / memory / disk
alarms for each one automatically.

In addition, an "Alarm Provisioner" Lambda is deployed and wired to an
EventBridge rule that fires on every EC2 instance state change:
  - Instance launched  (state = "running")                  → creates 3 alarms.
  - Instance terminated (state = "terminated"/"shutting-down") → deletes them.

This means you NEVER have to touch cdk.json or re-run ``cdk deploy``
just because you launched or terminated an EC2 instance.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import boto3
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

# ── Default thresholds (override via CDK context: -c cpu_threshold=80) ──
_DEFAULT_CPU  = 90
_DEFAULT_MEM  = 85
_DEFAULT_DISK = 90

# ── CloudWatch Agent config — embedded in SSM Association commands ────────
# append_dimensions adds InstanceId so our alarms can filter by it.
_CW_AGENT_CONFIG_JSON = json.dumps({
    "metrics": {
        "namespace": "CWAgent",
        "append_dimensions": {"InstanceId": "${aws:InstanceId}"},
        "aggregation_dimensions": [
            ["InstanceId"],          # mem_used_percent  → alarm matches {InstanceId}
            ["InstanceId", "path"],  # disk_used_percent → alarm matches {InstanceId, path}
        ],
        "metrics_collected": {
            "mem": {
                "measurement": ["mem_used_percent"],
                "metrics_collection_interval": 60,
            },
            "disk": {
                "measurement": ["disk_used_percent"],
                "resources": ["/"],
                "metrics_collection_interval": 60,
            },
        },
    }
}, separators=(",", ":"))  # compact — no spaces, avoids shell quoting issues


# ── Helpers ───────────────────────────────────────────────────────────────

def _discover_instance_ids(region: str) -> list[str]:
    """Call EC2 at synth time to discover all instances in the account.

    Returns an empty list (with a warning) if the API call fails, so
    ``cdk synth`` still succeeds in CI environments without credentials.
    The Alarm Provisioner Lambda will handle alarms for all future instances.
    """
    try:
        ec2 = boto3.client("ec2", region_name=region)
        paginator = ec2.get_paginator("describe_instances")
        ids: list[str] = []
        for page in paginator.paginate(
            Filters=[
                {"Name": "instance-state-name", "Values": ["running", "stopped", "stopping"]}
            ]
        ):
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    ids.append(inst["InstanceId"])
        print(f"[MonitoringStack] Auto-discovered {len(ids)} EC2 instance(s): {ids}")
        return ids
    except Exception as exc:  # noqa: BLE001
        print(f"[MonitoringStack] WARNING: Could not auto-discover instances — {exc}")
        print("[MonitoringStack] Continuing without per-instance alarms at synth time.")
        return []


def _create_instance_alarms(
    stack: cdk.Stack,
    iid: str,
    idx: int,
    cpu: int,
    mem: int,
    disk: int,
) -> list[cw.Alarm]:
    """Create the 3 standard alarms (CPU / memory / disk) for one instance."""
    short = iid[-8:]

    cpu_alarm = cw.Alarm(
        stack, f"HighCpu-{idx}",
        alarm_name=f"devops-agent-high-cpu-{short}",
        alarm_description=f"CPU > {cpu}% for {iid}",
        metric=cw.Metric(
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            dimensions_map={"InstanceId": iid},
            period=cdk.Duration.minutes(5),
            statistic="Average",
        ),
        threshold=cpu,
        evaluation_periods=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.MISSING,
    )

    mem_alarm = cw.Alarm(
        stack, f"HighMem-{idx}",
        alarm_name=f"devops-agent-high-memory-{short}",
        alarm_description=f"Memory > {mem}% for {iid}",
        metric=cw.Metric(
            namespace="CWAgent",
            metric_name="mem_used_percent",
            dimensions_map={"InstanceId": iid},
            period=cdk.Duration.minutes(5),
            statistic="Average",
        ),
        threshold=mem,
        evaluation_periods=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.MISSING,
    )

    disk_alarm = cw.Alarm(
        stack, f"HighDisk-{idx}",
        alarm_name=f"devops-agent-high-disk-{short}",
        alarm_description=f"Disk > {disk}% for {iid}",
        metric=cw.Metric(
            namespace="CWAgent",
            metric_name="disk_used_percent",
            dimensions_map={"InstanceId": iid, "path": "/"},
            period=cdk.Duration.minutes(5),
            statistic="Average",
        ),
        threshold=disk,
        evaluation_periods=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.MISSING,
    )

    return [cpu_alarm, mem_alarm, disk_alarm]




# ── Stack ─────────────────────────────────────────────────────────────────

class MonitoringStack(cdk.Stack):
    """CloudWatch alarms + EventBridge rules for automated monitoring."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(scope, construct_id, **kwargs)

        region: str = self.region

        # Thresholds — override via CDK context: -c cpu_threshold=80
        cpu_threshold  = int(self.node.try_get_context("cpu_threshold")  or _DEFAULT_CPU)
        mem_threshold  = int(self.node.try_get_context("mem_threshold")  or _DEFAULT_MEM)
        disk_threshold = int(self.node.try_get_context("disk_threshold") or _DEFAULT_DISK)

        # ── Auto-discover all EC2 instances at synth time ─────────────
        instance_ids = _discover_instance_ids(region)

        self.alarms: list[cw.Alarm] = []
        for idx, iid in enumerate(instance_ids):
            self.alarms.extend(
                _create_instance_alarms(self, iid, idx, cpu_threshold, mem_threshold, disk_threshold)
            )

        # ── EventBridge rule — ANY alarm ALARM transition ─────────────
        # No alarmName filter so the agent handles every alarm in the
        # account, including ones created by the Provisioner Lambda for
        # instances launched after the last deploy.
        self.alarm_rule = events.Rule(
            self,
            "AlarmStateChangeRule",
            rule_name="devops-agent-alarm-trigger",
            description="Fires when ANY CloudWatch alarm in the account enters ALARM state",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                detail={"state": {"value": ["ALARM"]}},
            ),
        )

        # ── Alarm Provisioner Lambda ──────────────────────────────────
        # Automatically creates / deletes the 3 standard alarms whenever
        # an EC2 instance is launched or terminated in the account.
        self.provisioner_fn = _lambda.Function(
            self,
            "AlarmProvisioner",
            function_name="devops-agent-alarm-provisioner",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=cdk.Duration.seconds(60),
            memory_size=128,
            log_group=logs.LogGroup(
                self, "ProvisionerLogs",
                log_group_name="/aws/lambda/devops-agent-alarm-provisioner",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            ),
            environment={
                "CPU_THRESHOLD":  str(cpu_threshold),
                "MEM_THRESHOLD":  str(mem_threshold),
                "DISK_THRESHOLD": str(disk_threshold),
            },
            code=_lambda.InlineCode(_PROVISIONER_CODE),
        )

        # Provisioner needs to create / delete CloudWatch alarms
        self.provisioner_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudwatch:PutMetricAlarm",
                    "cloudwatch:DeleteAlarms",
                    "cloudwatch:DescribeAlarms",
                    "ec2:DescribeInstances",
                ],
                resources=["*"],
            )
        )

        # EventBridge rule watching every EC2 state change in the account
        ec2_rule = events.Rule(
            self,
            "Ec2StateChangeRule",
            rule_name="devops-agent-ec2-state-change",
            description=(
                "Triggers the Alarm Provisioner Lambda when any EC2 instance "
                "is launched or terminated — alarms are always in sync."
            ),
            event_pattern=events.EventPattern(
                source=["aws.ec2"],
                detail_type=["EC2 Instance State-change Notification"],
                detail={"state": ["running", "terminated", "shutting-down"]},
            ),
        )
        ec2_rule.add_target(targets.LambdaFunction(self.provisioner_fn))

        # ── SSM State Manager — auto-install CloudWatch Agent ─────────
        # Targets ALL managed instances (InstanceIds=["*"]).
        # apply_only_at_cron_interval=False means SSM also runs this
        # association the moment a NEW instance registers — fully automatic.
        ssm.CfnAssociation(
            self,
            "InstallConfigureCWAgent",
            name="AWS-RunShellScript",
            association_name="devops-agent-cloudwatch-agent-setup",
            # "*" = every SSM-managed instance in the account
            targets=[{"key": "InstanceIds", "values": ["*"]}],
            parameters={
                "commands": [
                    # Install agent (yum for Amazon Linux / RHEL, apt for Ubuntu)
                    "sudo yum install -y amazon-cloudwatch-agent 2>/dev/null "
                    "|| sudo apt-get install -y -q amazon-cloudwatch-agent 2>/dev/null "
                    "|| true",
                    # Write config (single-quoted so ${aws:InstanceId} is NOT expanded by shell;
                    # the CW Agent itself resolves this placeholder at runtime)
                    f"printf '%s' '{_CW_AGENT_CONFIG_JSON}' "
                    "| sudo tee /tmp/devops-cw-config.json > /dev/null",
                    # Apply config and (re)start the agent
                    "sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl "
                    "-a fetch-config -m ec2 -s -c file:/tmp/devops-cw-config.json",
                    # Print status so SSM command output shows health
                    "sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl "
                    "-m ec2 -a status",
                ]
            },
            # Re-apply daily so any drift (agent stopped, config changed) is self-healed
            schedule_expression="rate(1 day)",
            # False = run immediately on new instances, not just on the next scheduled tick
            apply_only_at_cron_interval=False,
        )

        # ── Outputs ───────────────────────────────────────────────────
        cdk.CfnOutput(
            self, "DiscoveredInstances",
            value=json.dumps(instance_ids) if instance_ids else "[]",
            description="EC2 instances discovered at synth time",
        )
        cdk.CfnOutput(
            self, "AlarmCount",
            value=str(len(self.alarms)),
            description="Alarms created at synth time (3 per instance)",
        )
        cdk.CfnOutput(self, "EventRuleArn", value=self.alarm_rule.rule_arn)
        cdk.CfnOutput(
            self, "AlarmProvisionerArn",
            value=self.provisioner_fn.function_arn,
            description="Lambda that auto-creates/deletes alarms for new/terminated instances",
        )

    def add_lambda_target(self, fn: _lambda.Function) -> None:
        """Wire the EventBridge alarm rule to invoke a Lambda function."""
        self.alarm_rule.add_target(targets.LambdaFunction(fn))


# ── Alarm Provisioner inline Lambda code ─────────────────────────────────
# Kept inline (no external dependencies beyond boto3) so it does not need
# a separate deployment bundle.

_PROVISIONER_CODE = """\
import boto3
import json
import os

CPU_THRESHOLD  = int(os.environ.get("CPU_THRESHOLD",  "90"))
MEM_THRESHOLD  = int(os.environ.get("MEM_THRESHOLD",  "85"))
DISK_THRESHOLD = int(os.environ.get("DISK_THRESHOLD", "90"))

ALARM_PERIOD = 300   # 5 minutes
EVAL_PERIODS = 2


def _alarm_names(short):
    return [
        f"devops-agent-high-cpu-{short}",
        f"devops-agent-high-memory-{short}",
        f"devops-agent-high-disk-{short}",
    ]


def create_alarms(cw, instance_id):
    short = instance_id[-8:]
    print(f"Creating alarms for {instance_id}")
    common = dict(
        EvaluationPeriods=EVAL_PERIODS,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="missing",
        Period=ALARM_PERIOD,
        Statistic="Average",
    )
    cw.put_metric_alarm(
        AlarmName=f"devops-agent-high-cpu-{short}",
        AlarmDescription=f"CPU > {CPU_THRESHOLD}% for {instance_id}",
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        Threshold=CPU_THRESHOLD,
        **common,
    )
    cw.put_metric_alarm(
        AlarmName=f"devops-agent-high-memory-{short}",
        AlarmDescription=f"Memory > {MEM_THRESHOLD}% for {instance_id}",
        Namespace="CWAgent",
        MetricName="mem_used_percent",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        Threshold=MEM_THRESHOLD,
        **common,
    )
    cw.put_metric_alarm(
        AlarmName=f"devops-agent-high-disk-{short}",
        AlarmDescription=f"Disk > {DISK_THRESHOLD}% for {instance_id}",
        Namespace="CWAgent",
        MetricName="disk_used_percent",
        Dimensions=[
            {"Name": "InstanceId", "Value": instance_id},
            {"Name": "path",       "Value": "/"},
        ],
        Threshold=DISK_THRESHOLD,
        **common,
    )
    print(f"3 alarms created for {instance_id}")


def delete_alarms(cw, instance_id):
    short = instance_id[-8:]
    names = _alarm_names(short)
    print(f"Deleting alarms {names} for terminated instance {instance_id}")
    cw.delete_alarms(AlarmNames=names)
    print(f"Alarms deleted for {instance_id}")


def handler(event, context):
    instance_id = event["detail"]["instance-id"]
    state       = event["detail"]["state"]
    print(f"EC2 state change: {instance_id} -> {state}")

    cw = boto3.client("cloudwatch")

    if state == "running":
        create_alarms(cw, instance_id)
    elif state in ("terminated", "shutting-down"):
        delete_alarms(cw, instance_id)
    else:
        print(f"No action needed for state={state}")

    return {"statusCode": 200, "body": json.dumps({"instance_id": instance_id, "state": state})}
"""

 
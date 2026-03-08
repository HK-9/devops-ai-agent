"""
Monitoring Stack — CloudWatch alarms and EventBridge rules.

Creates CPU utilization alarms and an EventBridge rule that triggers
the agent Lambda when an alarm fires.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as _lambda
from constructs import Construct


class MonitoringStack(cdk.Stack):
    """CloudWatch alarms + EventBridge rules for automated monitoring."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(scope, construct_id, **kwargs)

        # ── Parameters ───────────────────────────────────────────────
        monitored_instance_id = self.node.try_get_context("monitored_instance_id") or "i-0000000000example"
        
        instance_id_param = cdk.CfnParameter(
            self,
            "MonitoredInstanceId",
            type="String",
            description="EC2 Instance ID to monitor",
            default=monitored_instance_id,
        )

        cpu_threshold = cdk.CfnParameter(
            self,
            "CpuThreshold",
            type="Number",
            description="CPU utilization threshold (%)",
            default=80,
        )

        # ── CloudWatch Alarm ─────────────────────────────────────────
        self.cpu_alarm = cw.Alarm(
            self,
            "HighCpuAlarm",
            alarm_name="devops-agent-high-cpu",
            alarm_description=(
                f"CPU utilization exceeds {cpu_threshold.value_as_number}% "
                f"for instance {instance_id_param.value_as_string}"
            ),
            metric=cw.Metric(
                namespace="AWS/EC2",
                metric_name="CPUUtilization",
                dimensions_map={"InstanceId": instance_id_param.value_as_string},
                period=cdk.Duration.minutes(5),
                statistic="Average",
            ),
            threshold=cpu_threshold.value_as_number,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.MISSING,
        )

        # ── EventBridge Rule ─────────────────────────────────────────
        self.alarm_rule = events.Rule(
            self,
            "AlarmStateChangeRule",
            rule_name="devops-agent-alarm-trigger",
            description="Fires when a monitored CloudWatch alarm enters ALARM state",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                detail={
                    "state": {"value": ["ALARM"]},
                    "alarmName": [self.cpu_alarm.alarm_name],
                },
            ),
        )

        # ── Outputs ──────────────────────────────────────────────────
        cdk.CfnOutput(self, "AlarmArn", value=self.cpu_alarm.alarm_arn)
        cdk.CfnOutput(self, "EventRuleArn", value=self.alarm_rule.rule_arn)

    def add_lambda_target(self, fn: _lambda.Function) -> None:
        """Wire the EventBridge rule to invoke a Lambda function."""
        self.alarm_rule.add_target(targets.LambdaFunction(fn))

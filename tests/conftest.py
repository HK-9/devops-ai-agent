"""
Shared test fixtures for the DevOps AI Agent test suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_instance_id() -> str:
    """A consistent fake instance ID for tests."""
    return "i-0abc123def456789a"


@pytest.fixture
def sample_alarm_event() -> dict:
    """A realistic EventBridge CloudWatch alarm event."""
    return {
        "version": "0",
        "id": "12345678-1234-1234-1234-123456789012",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "account": "123456789012",
        "time": "2026-03-07T10:00:00Z",
        "region": "us-east-1",
        "resources": ["arn:aws:cloudwatch:us-east-1:123456789012:alarm:devops-agent-high-cpu"],
        "detail": {
            "alarmName": "devops-agent-high-cpu",
            "alarmDescription": "CPU utilization exceeds 80% for instance i-0abc123def456789a",
            "state": {
                "value": "ALARM",
                "reason": "Threshold Crossed: 1 out of 2 datapoints [92.35] was greater than the threshold (80.0).",
                "timestamp": "2026-03-07T10:00:00.000+0000",
            },
            "previousState": {
                "value": "OK",
                "reason": "Threshold Crossed: 1 out of 2 datapoints [42.10] was not greater than the threshold (80.0).",
                "timestamp": "2026-03-07T09:45:00.000+0000",
            },
            "configuration": {
                "threshold": 80.0,
                "comparisonOperator": "GreaterThanThreshold",
                "evaluationPeriods": 2,
                "metrics": [
                    {
                        "id": "cpu",
                        "metricStat": {
                            "metric": {
                                "namespace": "AWS/EC2",
                                "name": "CPUUtilization",
                                "dimensions": {
                                    "InstanceId": "i-0abc123def456789a",
                                },
                            },
                            "period": 300,
                            "stat": "Average",
                        },
                    }
                ],
            },
        },
    }

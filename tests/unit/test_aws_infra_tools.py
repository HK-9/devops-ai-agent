"""
Unit tests for AWS Infrastructure MCP tools.

Uses moto to mock the EC2 service so tests run without AWS credentials.
"""

from __future__ import annotations

import pytest
import boto3
from moto import mock_aws

from src.mcp_servers.aws_infra.tools import (
    describe_ec2_instance,
    list_ec2_instances,
    restart_ec2_instance,
)


@pytest.fixture
def aws_credentials(monkeypatch):
    """Set dummy AWS credentials for moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def ec2_with_instances(aws_credentials):
    """Create a mocked EC2 environment with some instances."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        # Launch two instances
        response = ec2.run_instances(
            ImageId="ami-12345678",
            InstanceType="t3.medium",
            MinCount=2,
            MaxCount=2,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": "test-server"},
                        {"Key": "Environment", "Value": "test"},
                    ],
                }
            ],
        )
        instance_ids = [inst["InstanceId"] for inst in response["Instances"]]
        yield {"client": ec2, "instance_ids": instance_ids}


@pytest.mark.unit
class TestListEc2Instances:
    """Tests for the list_ec2_instances tool."""

    @pytest.mark.asyncio
    async def test_list_running_instances(self, ec2_with_instances):
        result = await list_ec2_instances(state_filter="running")
        assert "instances" in result
        assert result["count"] >= 2

    @pytest.mark.asyncio
    async def test_list_with_tag_filter(self, ec2_with_instances):
        result = await list_ec2_instances(
            state_filter="running",
            tag_filters={"Environment": "test"},
        )
        assert "instances" in result
        for inst in result["instances"]:
            assert inst["tags"].get("Environment") == "test"

    @pytest.mark.asyncio
    async def test_list_stopped_returns_empty(self, ec2_with_instances):
        result = await list_ec2_instances(state_filter="stopped")
        assert result["count"] == 0


@pytest.mark.unit
class TestDescribeEc2Instance:
    """Tests for the describe_ec2_instance tool."""

    @pytest.mark.asyncio
    async def test_describe_existing_instance(self, ec2_with_instances):
        iid = ec2_with_instances["instance_ids"][0]
        result = await describe_ec2_instance(iid)
        assert result["instance_id"] == iid
        assert result["state"] == "running"
        assert "security_groups" in result

    @pytest.mark.asyncio
    async def test_describe_nonexistent_instance(self, ec2_with_instances):
        result = await describe_ec2_instance("i-nonexistent")
        assert result.get("error") is True


@pytest.mark.unit
class TestRestartEc2Instance:
    """Tests for the restart_ec2_instance tool."""

    @pytest.mark.asyncio
    async def test_restart_instance(self, ec2_with_instances):
        iid = ec2_with_instances["instance_ids"][0]
        result = await restart_ec2_instance(iid)
        assert result.get("action") == "restart"
        assert result["instance_id"] == iid

"""
SigV4 request signing for httpx — used by the MCP client to authenticate
against the AgentCore Gateway when it is configured with AWS_IAM auth.

Uses botocore (bundled with boto3) so no additional dependencies are needed.
"""

from __future__ import annotations

import io
from urllib.parse import urlparse

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession


class BotoSigV4Auth(httpx.Auth):
    """httpx.Auth implementation that signs every request with AWS SigV4.

    Parameters
    ----------
    region : str
        AWS region (e.g. ``ap-southeast-2``).
    service : str
        The AWS service name used in the signing scope
        (``bedrock-agentcore`` for AgentCore gateways).
    """

    def __init__(self, region: str, service: str = "bedrock-agentcore") -> None:
        self._region = region
        self._service = service
        # botocore session auto-discovers credentials from the standard
        # chain: env vars → instance profile → container credentials, etc.
        self._session = BotocoreSession()

    def auth_flow(self, request: httpx.Request):
        """Sign the outgoing request and yield it."""
        # Resolve credentials lazily so container/role credentials
        # that rotate are always fresh.
        credentials = self._session.get_credentials().get_frozen_credentials()

        # Build a botocore AWSRequest from the httpx Request
        url = str(request.url)
        body = request.content if request.content else b""
        headers = dict(request.headers)
        # Remove host header — SigV4Auth re-computes it
        headers.pop("host", None)

        aws_request = AWSRequest(
            method=request.method,
            url=url,
            data=body,
            headers=headers,
        )

        SigV4Auth(credentials, self._service, self._region).add_auth(aws_request)

        # Copy the signed headers back onto the httpx request
        for key, value in aws_request.headers.items():
            request.headers[key] = value

        yield request

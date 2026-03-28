"""
Setup Cognito OAuth for gateway → runtime authentication,
then register all 4 runtimes as gateway targets.

Steps:
  1. Create a Cognito domain (needed for /oauth2/token endpoint)
  2. Create a resource server with a scope (needed for client_credentials)
  3. Create a NEW app client with client_credentials flow + secret
  4. Register an OAuth2 credential provider in AgentCore
  5. Create 4 gateway targets using the OAuth credential provider
  6. Synchronize the gateway
"""

import json
import sys
import time
import urllib.parse

import boto3

REGION = "ap-southeast-2"
USER_POOL_ID = "ap-southeast-2_OmD4OzAYI"
GATEWAY_ID = "devopsagentgateway-0xdeyhdvbs"
DOMAIN_PREFIX = "devops-agent-mcp"  # becomes devops-agent-mcp.auth.ap-southeast-2.amazoncognito.com
RESOURCE_SERVER_ID = "mcp-tools"
RESOURCE_SERVER_SCOPE = "invoke"

DATA_PLANE_HOST = f"https://bedrock-agentcore.{REGION}.amazonaws.com"

TARGETS = [
    {
        "name": "aws-infra",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/mcp_server-4kjU5oAHWM",
        "description": "EC2 infrastructure tools: list, describe, restart instances",
    },
    {
        "name": "monitoring",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/monitoring_server-CI86d62MYP",
        "description": "CloudWatch metrics: CPU, memory, disk usage",
    },
    {
        "name": "teams",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/teams_server-hbrhm38Ef3",
        "description": "Microsoft Teams notifications: messages and incident cards",
    },
    {
        "name": "sns",
        "runtime_arn": "arn:aws:bedrock-agentcore:ap-southeast-2:650251690796:runtime/sns_server-2f8klN8rTF",
        "description": "Alert failover: Teams primary, SNS fallback",
    },
]


def wait_for_target_ready(client, gateway_id: str, target_id: str, timeout: int = 120) -> str:
    """Poll until a gateway target reaches READY (or terminal) status."""
    start = time.time()
    while time.time() - start < timeout:
        resp = client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        )
        status = resp["status"]
        reasons = resp.get("statusReasons", [])
        print(f"    status: {status}")
        if status == "READY":
            return status
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"):
            print(f"    ERROR — terminal status: {status}, reasons: {reasons}")
            return status
        time.sleep(5)
    print(f"    TIMEOUT after {timeout}s")
    return "TIMEOUT"


def setup_cognito():
    """Configure Cognito for client_credentials OAuth flow. Returns (client_id, client_secret)."""
    cognito = boto3.client("cognito-idp", region_name=REGION)

    # --- Step 1: Create domain ---
    print("\n[Cognito] Step 1: Setting up domain...")
    try:
        cognito.create_user_pool_domain(
            Domain=DOMAIN_PREFIX,
            UserPoolId=USER_POOL_ID,
        )
        print(f"  Created domain: {DOMAIN_PREFIX}.auth.{REGION}.amazoncognito.com")
    except cognito.exceptions.InvalidParameterException as e:
        if "already exists" in str(e).lower():
            print(f"  Domain already exists — OK")
        else:
            raise
    except Exception as e:
        if "already" in str(e).lower() or "exists" in str(e).lower():
            print(f"  Domain already exists — OK")
        else:
            raise

    # --- Step 2: Create resource server ---
    print("\n[Cognito] Step 2: Creating resource server...")
    try:
        cognito.create_resource_server(
            UserPoolId=USER_POOL_ID,
            Identifier=RESOURCE_SERVER_ID,
            Name="MCP Tools Access",
            Scopes=[
                {
                    "ScopeName": RESOURCE_SERVER_SCOPE,
                    "ScopeDescription": "Invoke MCP tools via gateway",
                }
            ],
        )
        print(f"  Created resource server: {RESOURCE_SERVER_ID}/{RESOURCE_SERVER_SCOPE}")
    except Exception as e:
        if "already exists" in str(e).lower():
            print(f"  Resource server already exists — OK")
        else:
            raise

    # --- Step 3: Create a new app client with client_credentials ---
    print("\n[Cognito] Step 3: Creating OAuth app client...")
    scope = f"{RESOURCE_SERVER_ID}/{RESOURCE_SERVER_SCOPE}"

    # Check if we already created a gateway client
    existing_clients = cognito.list_user_pool_clients(UserPoolId=USER_POOL_ID, MaxResults=20)
    for c in existing_clients.get("UserPoolClients", []):
        if c.get("ClientName") == "gateway-oauth-client":
            # Fetch details including secret
            detail = cognito.describe_user_pool_client(
                UserPoolId=USER_POOL_ID, ClientId=c["ClientId"]
            )["UserPoolClient"]
            if "ClientSecret" in detail:
                print(f"  Reusing existing client: {detail['ClientId']}")
                return detail["ClientId"], detail["ClientSecret"]

    try:
        resp = cognito.create_user_pool_client(
            UserPoolId=USER_POOL_ID,
            ClientName="gateway-oauth-client",
            GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"],
            AllowedOAuthScopes=[scope],
            AllowedOAuthFlowsUserPoolClient=True,
            SupportedIdentityProviders=["COGNITO"],
        )
        new_client = resp["UserPoolClient"]
        client_id = new_client["ClientId"]
        client_secret = new_client["ClientSecret"]
        print(f"  Created client: {client_id}")
        print(f"  Secret: {'*' * 10} (hidden)")
        return client_id, client_secret
    except Exception as e:
        print(f"  ERROR creating client: {e}")
        raise


def create_oauth_credential_provider(client_id: str, client_secret: str) -> str:
    """Register an OAuth2 credential provider in AgentCore. Returns the provider ARN."""
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Check if provider already exists
    existing = ac.list_oauth2_credential_providers()
    providers = existing.get("credentialProviders", existing.get("oauth2CredentialProviders", []))
    for p in providers:
        if p.get("name") == "devops-gateway-cognito-oauth":
            arn = p.get("credentialProviderArn", "")
            print(f"\n[AgentCore] OAuth2 provider already exists: {arn}")
            return arn

    token_endpoint = f"https://{DOMAIN_PREFIX}.auth.{REGION}.amazoncognito.com/oauth2/token"
    authorization_endpoint = f"https://{DOMAIN_PREFIX}.auth.{REGION}.amazoncognito.com/oauth2/authorize"
    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
    print(f"\n[AgentCore] Creating OAuth2 credential provider...")
    print(f"  Vendor: CognitoOauth2")
    print(f"  Issuer: {issuer}")
    print(f"  Token endpoint: {token_endpoint}")

    resp = ac.create_oauth2_credential_provider(
        name="devops-gateway-cognito-oauth",
        credentialProviderVendor="CognitoOauth2",
        oauth2ProviderConfigInput={
            "includedOauth2ProviderConfig": {
                "clientId": client_id,
                "clientSecret": client_secret,
                "issuer": issuer,
                "tokenEndpoint": token_endpoint,
                "authorizationEndpoint": authorization_endpoint,
            }
        },
    )
    provider_arn = resp.get("credentialProviderArn", resp.get("providerArn", "unknown"))
    print(f"  Credential Provider ARN: {provider_arn}")

    # Print full response to see what we get back
    for k, v in resp.items():
        if k != "ResponseMetadata":
            print(f"  {k}: {v}")

    return provider_arn


def register_targets(provider_arn: str):
    """Register the 4 runtimes as gateway targets using OAuth credentials."""
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    scope = f"{RESOURCE_SERVER_ID}/{RESOURCE_SERVER_SCOPE}"

    # Check existing targets first
    existing = ac.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)
    existing_items = existing.get("items", [])
    if existing_items:
        print(f"\n  Found {len(existing_items)} existing target(s):")
        for item in existing_items:
            print(f"    - {item['name']} (ID: {item['targetId']}, status: {item['status']})")
        answer = input("\n  Continue adding new targets? (y/n): ").strip().lower()
        if answer != "y":
            print("  Aborted.")
            sys.exit(0)

    created = []
    for t in TARGETS:
        print(f"\n{'=' * 60}")
        print(f"Registering target: {t['name']}")

        encoded_arn = urllib.parse.quote(t["runtime_arn"], safe="")
        mcp_endpoint = f"{DATA_PLANE_HOST}/runtimes/{encoded_arn}/invocations"
        print(f"  MCP endpoint: {mcp_endpoint}")

        try:
            resp = ac.create_gateway_target(
                gatewayIdentifier=GATEWAY_ID,
                name=t["name"],
                description=t["description"],
                targetConfiguration={
                    "mcp": {
                        "mcpServer": {
                            "endpoint": mcp_endpoint,
                        }
                    }
                },
                credentialProviderConfigurations=[
                    {
                        "credentialProviderType": "OAUTH",
                        "credentialProvider": {
                            "oauthCredentialProvider": {
                                "providerArn": provider_arn,
                                "scopes": [scope],
                                "grantType": "CLIENT_CREDENTIALS",
                            }
                        },
                    }
                ],
            )
            target_id = resp["targetId"]
            print(f"  Created target ID: {target_id}")
            print(f"  Waiting for READY...")
            status = wait_for_target_ready(ac, GATEWAY_ID, target_id)
            created.append({"name": t["name"], "target_id": target_id, "status": status})
        except Exception as exc:
            print(f"  FAILED: {exc}")
            created.append({"name": t["name"], "target_id": None, "status": f"ERROR: {exc}"})

    # Summary
    print(f"\n{'=' * 60}")
    print("REGISTRATION SUMMARY")
    print("=" * 60)
    all_ok = True
    for c in created:
        marker = "OK" if c["status"] == "READY" else "FAIL"
        print(f"  [{marker}] {c['name']}: {c['status']} (ID: {c['target_id']})")
        if c["status"] != "READY":
            all_ok = False

    if not all_ok:
        print("\nSome targets failed. Check the errors above.")
        sys.exit(1)

    # Sync gateway
    print(f"\nSynchronizing gateway...")
    try:
        ac.synchronize_gateway_targets(gatewayIdentifier=GATEWAY_ID)
        print("  Sync initiated. Waiting...")
        time.sleep(10)
        gw = ac.get_gateway(gatewayIdentifier=GATEWAY_ID)
        print(f"  Gateway status: {gw['status']}")
    except Exception as exc:
        print(f"  Sync warning: {exc}")

    print(f"\nGateway MCP URL: https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp")
    print("Done!")


def main():
    print("=" * 60)
    print("PHASE 4: Cognito OAuth Setup + Gateway Target Registration")
    print("=" * 60)

    # Step A: Setup Cognito
    client_id, client_secret = setup_cognito()

    # Step B: Create AgentCore OAuth credential provider
    provider_arn = create_oauth_credential_provider(client_id, client_secret)

    # Step C: Register targets with OAuth
    register_targets(provider_arn)


if __name__ == "__main__":
    main()

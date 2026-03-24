# ============================================================================
# Cognito User Pool Setup for AgentCore Runtime MCP Authentication
# ============================================================================
#
# This script creates a Cognito user pool, app client, and user for
# authenticating with AgentCore Runtime when deploying MCP servers.
#
# Run: .\scripts\setup_cognito.ps1
#
# After running, save the outputs:
#   - POOL_ID
#   - DISCOVERY_URL
#   - CLIENT_ID
#   - BEARER_TOKEN
# ============================================================================

param(
    [string]$Region = "ap-southeast-2",
    [string]$PoolName = "devops-agent-mcp-auth",
    [string]$Username = "devops-agent-user",
    [string]$Password = "DevOpsAgent2026!"
)

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Cognito Setup for AgentCore MCP Authentication" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Region:   $Region"
Write-Host "  Pool:     $PoolName"
Write-Host "  Username: $Username"
Write-Host ""

# ── Step 1: Create User Pool ──────────────────────────────────────────────
Write-Host "→ Step 1: Creating Cognito User Pool..." -ForegroundColor Yellow

$poolResult = aws cognito-idp create-user-pool `
    --pool-name $PoolName `
    --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' `
    --region $Region `
    --output json | ConvertFrom-Json

$PoolId = $poolResult.UserPool.Id

if (-not $PoolId) {
    Write-Host "✗ Failed to create user pool" -ForegroundColor Red
    exit 1
}
Write-Host "✓ User Pool created: $PoolId" -ForegroundColor Green

# ── Step 2: Create App Client ─────────────────────────────────────────────
Write-Host "→ Step 2: Creating App Client..." -ForegroundColor Yellow

$clientResult = aws cognito-idp create-user-pool-client `
    --user-pool-id $PoolId `
    --client-name "devops-agent-mcp-client" `
    --no-generate-secret `
    --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" `
    --region $Region `
    --output json | ConvertFrom-Json

$ClientId = $clientResult.UserPoolClient.ClientId

if (-not $ClientId) {
    Write-Host "✗ Failed to create app client" -ForegroundColor Red
    exit 1
}
Write-Host "✓ App Client created: $ClientId" -ForegroundColor Green

# ── Step 3: Create User ──────────────────────────────────────────────────
Write-Host "→ Step 3: Creating user '$Username'..." -ForegroundColor Yellow

aws cognito-idp admin-create-user `
    --user-pool-id $PoolId `
    --username $Username `
    --region $Region `
    --message-action SUPPRESS `
    --output json | Out-Null

Write-Host "✓ User created" -ForegroundColor Green

# ── Step 4: Set Permanent Password ────────────────────────────────────────
Write-Host "→ Step 4: Setting permanent password..." -ForegroundColor Yellow

aws cognito-idp admin-set-user-password `
    --user-pool-id $PoolId `
    --username $Username `
    --password $Password `
    --region $Region `
    --permanent | Out-Null

Write-Host "✓ Password set" -ForegroundColor Green

# ── Step 5: Authenticate & Get Bearer Token ───────────────────────────────
Write-Host "→ Step 5: Authenticating to get bearer token..." -ForegroundColor Yellow

$authResult = aws cognito-idp initiate-auth `
    --client-id $ClientId `
    --auth-flow USER_PASSWORD_AUTH `
    --auth-parameters "USERNAME=$Username,PASSWORD=$Password" `
    --region $Region `
    --output json | ConvertFrom-Json

$BearerToken = $authResult.AuthenticationResult.AccessToken

if (-not $BearerToken) {
    Write-Host "✗ Failed to authenticate" -ForegroundColor Red
    exit 1
}
Write-Host "✓ Bearer token obtained" -ForegroundColor Green

# ── Output ────────────────────────────────────────────────────────────────
$DiscoveryUrl = "https://cognito-idp.$Region.amazonaws.com/$PoolId/.well-known/openid-configuration"

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  ✓ Cognito Setup Complete!" -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  Save these values for agentcore configure:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  POOL_ID       = $PoolId"
Write-Host "  CLIENT_ID     = $ClientId"
Write-Host "  DISCOVERY_URL = $DiscoveryUrl"
Write-Host "  BEARER_TOKEN  = $($BearerToken.Substring(0, 20))...  (truncated)"
Write-Host ""
Write-Host "  To set as environment variables:" -ForegroundColor DarkGray
Write-Host "  `$env:POOL_ID = `"$PoolId`"" -ForegroundColor DarkGray
Write-Host "  `$env:CLIENT_ID = `"$ClientId`"" -ForegroundColor DarkGray
Write-Host "  `$env:DISCOVERY_URL = `"$DiscoveryUrl`"" -ForegroundColor DarkGray
Write-Host "  `$env:BEARER_TOKEN = `"$BearerToken`"" -ForegroundColor DarkGray
Write-Host ""

# Export to environment for current session
$env:POOL_ID = $PoolId
$env:CLIENT_ID = $ClientId
$env:DISCOVERY_URL = $DiscoveryUrl
$env:BEARER_TOKEN = $BearerToken

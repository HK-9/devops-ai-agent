The Flow: 

lambda_handler.py
 → Every AWS Touchpoint
CloudWatch Alarm → EventBridge → Lambda Handler → Event Parser → Agent Core → MCP Client
                                                                      ↕              ↕
                                                              Bedrock API     EC2 / CloudWatch / Teams
Integration #1: AWS Credentials (local dev)
Code: 

aws_helpers.py#L68
 — boto3.Session(region_name="ap-southeast-2")
Do: Run aws configure with keys that have access to ap-southeast-2
Integration #2: Bedrock Agent (your .env already has IDs)
Code: 

agent_core.py#L130
 — checks AGENT_ID=KYZ4EKSMX5 + AGENT_ALIAS_ID=LFVTIWMNFK
Do: ⚠️ Verify these exist in Bedrock Console in ap-southeast-2 and the alias status is "Prepared"
Integration #3: EC2 instances (for the tools to query)
Code: 

aws_infra/tools.py#L36
 — ec2.describe_instances()
Do: Need at least one running EC2 instance in ap-southeast-2
Integration #4: CloudWatch metrics (auto-collected for running instances)
Code: 

monitoring/tools.py#L37
 — cloudwatch.get_metric_statistics()
Do: CPU metrics auto-exist. Memory/disk require CloudWatch Agent installed on EC2.
Integration #5: Teams Webhook URL (currently empty ❌)
Code: 

teams_webhook.py#L37
 — settings.teams_webhook_url
Do: Teams → Channel → Connectors → Incoming Webhook → paste URL into 

.env
Integration #6: CloudWatch Alarm (triggers the whole pipeline)
Code: 

event_parser.py#L52
 — expects source: "aws.cloudwatch"
Do: Create alarm on CPUUtilization > 80% for your instance (CDK or Console)
Integration #7: EventBridge → Lambda wiring (⚠️ code bug)
Code: 

infra/app.py#L39
 — missing monitoring.add_lambda_target(agent_runner.agent_fn)
Do: Add that line to 

app.py
, or wire manually in EventBridge Console
Integration #8: Lambda env vars (not all are set in CDK)
Code: 

agent_runner_stack.py#L33
 — only sets LOG_LEVEL, LOG_FORMAT
Do: Add AGENT_ID, AGENT_ALIAS_ID, TEAMS_WEBHOOK_URL, AWS_REGION to the Lambda environment
The full guide with console steps, IAM policy JSONs, and your 

.env
 status is in 
aws_integrations_guide.md
.

Immediate next step: Verify your Bedrock agent exists → aws bedrock-agent get-agent --agent-id KYZ4EKSMX5 --region ap-southeast-2. Want me to run that?


## v0.1.0 (2026-03-13)

### Feat

- add is_alarm parameter to invoke method to handle alarm flows
- add policy.json for Bedrock Agent runtime access and enforce inline agent mode in DevOpsAgent

### Fix

- exclude list_ec2_instances from tool definitions to prevent redundant calls
- change the tag_filters schema to also accept a string type.
- resolve lint errors, type issues, and test failures for CI

### Refactor

- improve alarm notification message format and instructions for handling CloudWatch alarms
- increased reasoning loops
- load environment variables from .env file and clean up AGENT_ID settings

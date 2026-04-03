# DevOps AI Agent — Tool Reference

## CPU Tools

| Tool | Parameters | What It Does |
|------|-----------|--------------|
| `diagnose_instance_tool` | `instance_id` | Runs SSM on the instance to collect top CPU processes, memory usage, disk usage, and system info in one combined command |
| `get_cpu_metrics_tool` | `instance_id`, `minutes` | Fetches CloudWatch CPUUtilization metrics for the instance over the last N minutes |
| `get_cpu_metrics_for_instances_tool` | `instance_ids[]` | Fetches CPU metrics for multiple instances simultaneously |
| `remediate_high_cpu_tool` | `instance_id`, `pid` (int) | Kills the specified process on the instance via SSM (`kill -9 <pid>`) |

---

## Memory Tools

| Tool | Parameters | What It Does |
|------|-----------|--------------|
| `diagnose_instance_tool` | `instance_id` | Also collects memory stats (`free -m`, `top` output) alongside CPU during diagnosis |
| `get_memory_metrics_tool` | `instance_id`, `minutes` | Fetches CloudWatch `mem_used_percent` metrics (requires CW Agent installed on instance) |
| `remediate_high_memory_tool` | `instance_id`, `pid` (int) | Kills the specified memory-consuming process on the instance via SSM |

---

## Disk Tools

| Tool | Parameters | What It Does |
|------|-----------|--------------|
| `diagnose_instance_tool` | `instance_id` | Also collects disk usage (`df -h`) alongside CPU/memory during diagnosis |
| `get_disk_usage_tool` | `instance_id`, `minutes` | Fetches CloudWatch `disk_used_percent` metrics (requires CW Agent installed) |
| `remediate_disk_full_tool` | `instance_id` | Cleans up disk space on the instance via SSM — removes temp files, old logs, package caches |

---

## Notification & Approval Tools

| Tool | Parameters | What It Does |
|------|-----------|--------------|
| `send_alert_with_failover_tool` | `subject`, `message` | Sends an email via SNS. Used for AUTO-FIXED, NEEDS ATTENTION, and EXECUTED notifications |
| `request_approval_tool` | `instance_id`, `action_type`, `reason`, `details` | Stores approval request in DynamoDB and sends email with clickable APPROVE/REJECT links |
| `check_approval_status_tool` | `approval_id` | Checks DynamoDB to see if a human has clicked APPROVE or REJECT |
| `update_approval_status_tool` | `approval_id`, `status` | Updates DynamoDB approval record status (e.g., to "executed") |

---

## EC2 Management Tools

| Tool | Parameters | What It Does |
|------|-----------|--------------|
| `list_ec2_instances_tool` | `state_filter`, `tag_filters` | Lists EC2 instances filtered by state (running/stopped) or tags |
| `describe_ec2_instance_tool` | `instance_id` | Returns detailed info about an instance: type, state, IP, tags, launch time |
| `restart_ec2_instance_tool` | `instance_id` | Stops and starts the EC2 instance (used after APPROVE for MAJOR incidents) |
| `run_ssm_command_tool` | `instance_id`, `shell_command` | Runs an arbitrary shell command on the instance via SSM Session Manager |

---

## Teams Tools

| Tool | Parameters | What It Does |
|------|-----------|--------------|
| `send_teams_message_tool` | `message` | Posts a plain text message to the configured Microsoft Teams webhook |
| `create_incident_notification_tool` | `alarm_name`, `instance_id`, etc. | Posts a formatted incident card to Teams with alarm details |

---

## Tool Usage by Scenario

| Scenario | Tools Called (in order) |
|----------|------------------------|
| **MINOR CPU alarm** | `diagnose_instance` → `remediate_high_cpu` → `send_alert_with_failover` |
| **MINOR memory alarm** | `diagnose_instance` → `remediate_high_memory` → `send_alert_with_failover` |
| **MINOR disk alarm** | `diagnose_instance` → `remediate_disk_full` → `send_alert_with_failover` |
| **MAJOR alarm (any type)** | `diagnose_instance` → `request_approval` |
| **After APPROVE clicked** | `check_approval_status` → `restart_ec2_instance` → `update_approval_status` → `send_alert_with_failover` |
| **Diagnose fails (fallback)** | `get_cpu_metrics` → `send_alert_with_failover` |
| **Web chat: list instances** | `list_ec2_instances` |
| **Web chat: instance details** | `describe_ec2_instance` |
| **Web chat: CPU metrics** | `get_cpu_metrics` |
| **Web chat: memory metrics** | `get_memory_metrics` |
| **Web chat: disk metrics** | `get_disk_usage` |
| **Web chat: run command** | `run_ssm_command` |
| **Web chat: Teams notify** | `send_teams_message` or `create_incident_notification` |
 
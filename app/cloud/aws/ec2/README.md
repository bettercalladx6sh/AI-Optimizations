# EC2 discovery and analysis

Read-only discovery, change tracking and utilization analysis for the EC2 instances in the
AWS account configured in `.env`. Everything this module writes goes to the **local analysis
database** (docker compose). The company database is never written to.

All commands run from the project root with the virtual environment active:

```bash
source .venv/bin/activate
```

---

## Quick reference

| Command | Shorthand | What it does |
|---|---|---|
| `python -m app.cloud.aws.ec2 list` | `python -m app.cloud.aws.ec2` | List instances: status, VPC, IPs |
| `python -m app.cloud.aws.ec2 configuration` | `config` | Full specs of every instance |
| `python -m app.cloud.aws.ec2 configuration --resource ID` | `config -r ID` | Full specs of one instance (ID or Name tag) |
| `python -m app.cloud.aws.ec2 run [WINDOW]` | `analyze` | Analysis: configuration and CPU/memory, stored, with changes and readiness |
| `python -m app.cloud.aws.ec2 metrics [WINDOW]` | `usage` | Stored CPU/memory summary (no CloudWatch calls) |
| `python -m app.cloud.aws.ec2 metrics WINDOW -r ID --hourly` | `usage WINDOW -r ID -H` | Every hourly average of one instance |
| `python -m app.cloud.aws.ec2 history` | — | Stored configuration changes, newest first |
| `python -m app.cloud.aws.ec2 <command> --help` | `-h` | Options of any command |

Options shared by several commands:

| Option | Short | Commands | Meaning |
|---|---|---|---|
| `--region REGION` | — | all | Region to use (default: `AWS_DEFAULT_REGION` from `.env`) |
| `--state STATE` | — | `list`, `config` | Only this state; repeat for several (default: all except terminated) |
| `--resource ID_OR_NAME` | `-r` | `config`, `metrics`, `history` | One instance by ID (`i-...`) or Name tag |
| `--hourly` | `-H` | `metrics` | List every hourly average |
| `--limit COUNT` | `-n` | `history` | How many changes to show (default 20) |

States: `pending`, `running`, `shutting-down`, `stopping`, `stopped`, `terminated`.

---

## Analysis windows

`run` and `metrics` take a window as their first argument. The default is `30d`.

| Window | Purpose | Hourly datapoints per instance and metric |
|---|---|---|
| `1h` | testing | 1 |
| `1d` | testing | 24 |
| `3d` | testing | 72 |
| `7d` | testing | 168 |
| `30d` | production | 720 |
| `90d` | production | 2160 |

Windows are defined in `ANALYSIS_WINDOWS` in `analysis.py`; `DEFAULT_WINDOW` sets the default.

---

## Commands in detail

### `list`: instances at a glance

```bash
python -m app.cloud.aws.ec2
python -m app.cloud.aws.ec2 list --state running
python -m app.cloud.aws.ec2 list --state running --state stopped
python -m app.cloud.aws.ec2 list --region eu-west-1
```

Shows each instance's ID, name, status, VPC, public IP and private IP. Running instances come
first. `list` is the default when no command is given.

### `configuration` / `config`: full specs

```bash
python -m app.cloud.aws.ec2 config
python -m app.cloud.aws.ec2 config -r i-0abc123
python -m app.cloud.aws.ec2 config -r web-server-01
python -m app.cloud.aws.ec2 config --state running
```

Grouped into sections:
- **Overview:** ID, status, launch time, tags.
- **Compute:** type, vCPUs, memory, network, burstable, generation, architecture, platform, lifecycle, tenancy.
- **Network:** zone, VPC, subnet, IPs, security groups.
- **Storage:** root device, EBS optimized, each volume's size, type, IOPS, throughput and encryption.
- **Access & monitoring:** AMI, key pair, IAM profile, monitoring.

`-r` searches every state, so stopped and terminated instances can be inspected too.

### `run` / `analyze`: the analysis

```bash
python -m app.cloud.aws.ec2 run          # 30d, production
python -m app.cloud.aws.ec2 run 90d      # 90d, production
python -m app.cloud.aws.ec2 run 1h       # testing
python -m app.cloud.aws.ec2 run 1d
python -m app.cloud.aws.ec2 run 7d --region eu-west-1
```

One run does all of this, in order:

1. Reads every instance's configuration from EC2.
2. Collects **hourly CPU and memory averages** from CloudWatch for the whole window. The window ends at the last complete hour.
3. **First run** for an account and region: stores everything as the **baseline**.
   **Later runs:** compares with the stored configuration and stores every change.
4. Stores instances, configuration snapshots, tags, volumes, typed specs and metrics.
5. Reports **evaluation readiness**: an instance is ready only after a full window has passed since it was launched or last changed. Otherwise the output says which instances to wait for and until when.

Renames and detailed-monitoring changes are stored but do not restart the wait.

A run is recorded in `analysis_runs` before work starts, so a failed run stays visible with
its error message.

### `metrics` / `usage`: quick utilization view

```bash
python -m app.cloud.aws.ec2 metrics
python -m app.cloud.aws.ec2 metrics 7d
python -m app.cloud.aws.ec2 usage 1d -r api-01
python -m app.cloud.aws.ec2 usage 1d -r i-0abc123 -H
```

Reads **stored** hourly averages only; it makes no CloudWatch calls. Per instance and metric it
shows the average, the peak hourly average and hours collected (e.g. `719/720 hours (99.9%)`).
`-H` also lists every hour:

```text
  2026-11-08 18:00 UTC  CPU 10.0% · Memory 60.0%
  2026-11-08 19:00 UTC  CPU 11.0% · Memory 61.0%
```

Run an analysis first; `metrics` shows only what has been stored.

### `history`: stored changes

```bash
python -m app.cloud.aws.ec2 history
python -m app.cloud.aws.ec2 history -r i-0abc123
python -m app.cloud.aws.ec2 history -r api-01 -n 5
```

Shows every stored change (added, removed, modified) with old → new values, when it was
detected and by which run.

---

## Metrics collected

| Metric | CloudWatch namespace | Metric name | Dimension | Statistic | Period |
|---|---|---|---|---|---|
| CPU | `AWS/EC2` | `CPUUtilization` | `InstanceId` | Average | 1 hour |
| Memory | `EC2/Custom` | `mem_used_percent` | `InstanceId` | Average | 1 hour |

Memory is published by the **CloudWatch agent** into `EC2/Custom`. Instances without the agent
show `no memory data` instead of failing. Definitions are `CPU` and `MEMORY` in `metrics.py`.

---

## Related commands

These live outside this folder but are used with EC2:

```bash
python -m app.auth aws            # check AWS credentials from .env
python -m app.auth analysis-db    # check the local analysis database
python -m app.db status           # migrations and row counts per table
python -m app.db migrate          # apply pending migrations now (runs do it automatically)
docker compose up -d              # start the local analysis database
```

---

## Files in this folder

| File | Responsibility |
|---|---|
| `__main__.py` | Command line: `list`, `config`, `run`, `metrics`, `history` and their options |
| `__init__.py` | Public API for other modules (`run_analysis`, `list_instances`, `collect_metrics`, ...) |
| `instances.py` | `describe_instances()` (the single EC2 call), `list_instances()`, instance list output |
| `configuration.py` | `get_configurations()`: full specs, instance types (vCPU, memory) and EBS volumes |
| `changes.py` | `InstanceSnapshot`, `compare()`: detect added, removed and modified instances |
| `metrics.py` | `collect_metrics()`: hourly CPU and memory averages from CloudWatch |
| `store.py` | Writes instances, snapshots, tags, volumes, changes and metrics to the database |
| `analysis.py` | `run_analysis()`, readiness, `ANALYSIS_WINDOWS`, stored metrics and history output |

Shared code used here (not duplicated):

| File | Used for |
|---|---|
| `app/auth/aws_auth.py` | `get_client()` / `get_session()`: the only way to connect to AWS |
| `app/auth/mysql_auth.py` | Database connections (`ANALYSIS` profile for writes) |
| `app/cloud/aws/errors.py` | `aws_errors()`: boto errors turned into messages with IAM hints |
| `app/cloud/display.py` | Rich output: headers, sections, label/value lines |
| `app/db/database.py` | `session()` and automatic schema migrations |
| `app/db/resources.py` | Generic storage: accounts, runs, resources, snapshots, changes, tags |

---

## Database tables

All in the local analysis database. The schema is in `app/db/migrations/`.

| Table | Contents |
|---|---|
| `cloud_accounts` | Analysed AWS accounts |
| `analysis_runs` | Every run: window, purpose, status, counts, error |
| `resources` | Every instance: name, state, first/last seen, last changed, removed |
| `resource_snapshots` | Every distinct configuration an instance has had (full JSON) |
| `resource_changes` | Every detected change, linked to the before/after snapshots |
| `resource_tags` | Current tags |
| `ec2_instances` | Current specs in typed columns (type, family, size, vCPUs, memory, IPs, ...) |
| `ec2_volumes` | Current EBS volumes |
| `ec2_metric_datapoints` | Hourly CPU/memory averages: one row per instance, metric and hour |
| `ec2_metric_collections` | What each run collected per instance and metric (expected vs collected hours) |

The metrics tables include `instance_id` and `instance_name` so they can be queried directly:

```sql
SELECT period_start, average
FROM ec2_metric_datapoints
WHERE instance_id = 'i-0abc123' AND metric = 'cpu_utilization'
ORDER BY period_start;
```

To store new kinds of data, add a new numbered file such as `0004_<name>.sql` to
`app/db/migrations/`. Never edit a migration that has already been applied.

---

## Required IAM permissions (read-only)

| Permission | Used by |
|---|---|
| `sts:GetCallerIdentity` | account ID for every run |
| `ec2:DescribeInstances` | `list`, `config`, `run` |
| `ec2:DescribeInstanceTypes` | `config`, `run` (vCPUs, memory) |
| `ec2:DescribeVolumes` | `config`, `run` (EBS details) |
| `cloudwatch:GetMetricData` | `run` (CPU and memory) |

A missing permission is reported with the exact action to grant.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Failure (credentials, permissions, database, AWS error); the message and hints are printed |
| `2` | Invalid command or option |
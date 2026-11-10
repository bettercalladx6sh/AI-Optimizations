# EC2 discovery and analysis

Read-only discovery, change tracking and utilization analysis for the EC2 instances in the
AWS account configured in `.env`.

- **AWS is only read.** Nothing in the account is changed.
- **The company database is only read.** Everything this module writes goes to the
  **local analysis database** (docker compose).
- **CloudWatch is called as little as possible.** Every hour of every metric is fetched at most
  once. A run where nothing is missing makes **no CloudWatch calls**.

All commands run from the project root with the virtual environment active:

```bash
source .venv/bin/activate
```

---

## Contents

1. [Quick reference](#quick-reference)
2. [Analysis windows](#analysis-windows)
3. [Commands in detail](#commands-in-detail)
4. [How metrics are collected](#how-metrics-are-collected)
5. [The datapoint report](#the-datapoint-report)
6. [Where the data is stored](#where-the-data-is-stored)
7. [Inspecting stored data](#inspecting-stored-data)
8. [Files in this folder](#files-in-this-folder)
9. [Database migrations](#database-migrations)
10. [Required IAM permissions](#required-iam-permissions-read-only)
11. [Exit codes](#exit-codes)
12. [Known limitations](#known-limitations)

---

## Quick reference

| Command | Shorthand | What it does | CloudWatch |
|---|---|---|---|
| `python -m app.cloud.aws.ec2 list` | `python -m app.cloud.aws.ec2` | Instances: status, VPC, IPs | no |
| `python -m app.cloud.aws.ec2 configuration` | `config` | Full specs of every instance | no |
| `python -m app.cloud.aws.ec2 config -r ID` | | Full specs of one instance (ID or Name tag) | no |
| `python -m app.cloud.aws.ec2 run [WINDOW]` | `analyze` | Full analysis: configuration, changes, metrics, readiness | missing hours only |
| `python -m app.cloud.aws.ec2 metrics [WINDOW]` | `usage` | Quick analysis of CPU/memory for a window | missing hours only |
| `python -m app.cloud.aws.ec2 metrics -d DAY` | `usage -d DAY` | Quick analysis of one UTC day, hour by hour | missing hours only |
| `python -m app.cloud.aws.ec2 metrics -t HOUR` | `usage -t HOUR` | Quick analysis of one UTC hour | missing hours only |
| `python -m app.cloud.aws.ec2 metrics -r ID` | `usage -r ID` | Quick analysis of one instance | missing hours only |
| `python -m app.cloud.aws.ec2 metrics -o` | `usage -o` | Stored data only | **never** |
| `python -m app.cloud.aws.ec2 history` | | Stored configuration changes | no |
| `python -m app.cloud.aws.ec2 <command> --help` | `-h` | All options of a command | no |

### All options

| Option | Short | Commands | Meaning |
|---|---|---|---|
| `WINDOW` | | `run`, `metrics` | `1h`, `1d`, `3d`, `7d`, `30d` (default), `90d`; see [windows](#analysis-windows) |
| `--day DAY` | `-d` | `metrics` | One UTC day: `YYYY-MM-DD`, `today` or `yesterday`; lists every hour |
| `--hour HOUR` | `-t` | `metrics` | One UTC hour: `"YYYY-MM-DD HH"`, or `HH` for today |
| `--resource ID_OR_NAME` | `-r` | `config`, `metrics`, `history` | One instance by ID (`i-...`) or Name tag |
| `--hourly` | `-H` | `metrics` | Also list every hourly average |
| `--offline` | `-o` | `metrics` | Use stored data only; no CloudWatch calls |
| `--limit COUNT` | `-n` | `history` | How many changes to show (default 20) |
| `--state STATE` | | `list`, `config` | Only this state; repeat for several (default: all except terminated) |
| `--region REGION` | | all | Region to use (default: `AWS_DEFAULT_REGION` from `.env`) |

States: `pending`, `running`, `shutting-down`, `stopping`, `stopped`, `terminated`.

A `WINDOW` cannot be combined with `--day` or `--hour`, and `--day` cannot be combined with
`--hour`.

---

## Analysis windows

Defined in `app/cloud/windows.py` (`ANALYSIS_WINDOWS`, `DEFAULT_WINDOW = "30d"`), shared with Azure VMs.

| Window | Purpose | Covers | Example on 10 Nov 2026, 07:20 UTC |
|---|---|---|---|
| `30d` | production | **Current month to date**, from the 1st at 00:00 UTC | 1 Nov 00:00 → 10 Nov 07:00 (223 hours) |
| `90d` | production | **Current month and the two before it**, to date | 1 Sep 00:00 → 10 Nov 07:00 (1,687 hours) |
| `7d` | testing | Rolling 7 days | 3 Nov 07:00 → 10 Nov 07:00 (168 hours) |
| `3d` | testing | Rolling 3 days | 7 Nov 07:00 → 10 Nov 07:00 (72 hours) |
| `1d` | testing | Rolling 24 hours | 9 Nov 07:00 → 10 Nov 07:00 (24 hours) |
| `1h` | testing | The last settled hour | 10 Nov 06:00 → 07:00 (1 hour) |

- **Calendar windows start on the 1st** even when fewer than 30 days have passed: a `30d` run on
  10 November covers only 1–10 November.
- **Every window ends at the last settled hour.** An hour is settled once it ended at least
  **15 minutes** ago (`SETTLE_DELAY` in `metrics.py`). At 07:20 the last settled hour is
  06:00–07:00; at 07:10 it is 05:00–06:00.
- **Right after midnight on the 1st**, before any hour of the new month has settled, `30d` covers
  the previous month (the month of the last settled hour).
- **Readiness uses the nominal length.** An instance changed on 5 November must wait **30 days**
  (until 5 December) before a `30d` evaluation, and 90 days for `90d`.

---

## Commands in detail

### `list`: instances at a glance

```bash
python -m app.cloud.aws.ec2
python -m app.cloud.aws.ec2 list --state running
python -m app.cloud.aws.ec2 list --state running --state stopped
```

Instance ID, name, status, VPC, public IP and private IP. Running instances come first. `list`
is the default when no command is given.

### `configuration` / `config`: full specs

```bash
python -m app.cloud.aws.ec2 config
python -m app.cloud.aws.ec2 config -r i-0022548e58310083f
python -m app.cloud.aws.ec2 config -r prod-be-fe-001
```

Grouped into sections:
- **Overview:** ID, status, launch time, tags.
- **Compute:** type, vCPUs, memory, network, burstable, generation, architecture, platform, lifecycle, tenancy.
- **Network:** zone, VPC, subnet, IPs, security groups.
- **Storage:** root device, EBS optimized, volumes.
- **Access & monitoring:** AMI, key pair, IAM profile, monitoring.

`-r` searches every state.

### `run` / `analyze`: full analysis

```bash
python -m app.cloud.aws.ec2 run          # 30d: month to date (production)
python -m app.cloud.aws.ec2 run 90d      # last 3 calendar months to date (production)
python -m app.cloud.aws.ec2 run 1h       # testing
python -m app.cloud.aws.ec2 run 7d --region eu-west-1
```

One run, in order:

1. **Configuration.** Reads every instance from EC2 (`DescribeInstances`, `DescribeInstanceTypes`, `DescribeVolumes`).
2. **Plan.** Loads from the database which hours are already stored and which were already checked in CloudWatch.
3. **Fetch.** Requests **only the missing hours** of CPU and memory from CloudWatch. Nothing missing means no call.
4. **Changes.** The first run for an account and region stores the **baseline**. Later runs compare with the stored configuration and store every change.
5. **Store.** Saves instances, configuration snapshots, tags, volumes, typed specs, new datapoints, checked ranges and a per-run summary.
6. **Report.** Prints changes, the [datapoint report](#the-datapoint-report), utilization (average and peak hourly average) and **evaluation readiness**.

**Readiness:** an instance can be evaluated only after a full window has passed since it was
launched or last changed. Otherwise the output says which instances to wait for, and until when.
Renames and detailed-monitoring changes are stored but do not restart the wait.

A run is recorded in `analysis_runs` before any work, so a failed run stays visible with its error.

### `metrics` / `usage`: quick analysis

Quick CPU and memory view for stored instances. Like `run`, it fetches **only missing hours**,
but it skips the configuration and change checks, so it is faster.

```bash
python -m app.cloud.aws.ec2 metrics                              # month to date, all instances
python -m app.cloud.aws.ec2 metrics 7d                           # rolling 7 days
python -m app.cloud.aws.ec2 metrics -d yesterday                 # one day, every hour listed
python -m app.cloud.aws.ec2 metrics -d 2026-11-01                # a specific day
python -m app.cloud.aws.ec2 metrics -d 2026-11-01 -r mysql-proxy # one day, one instance
python -m app.cloud.aws.ec2 metrics -t "2026-11-10 06"           # one hour
python -m app.cloud.aws.ec2 metrics -t 06                        # 06:00-07:00 UTC today
python -m app.cloud.aws.ec2 metrics 30d -r i-0022548e58310083f -H  # one instance, every hour
python -m app.cloud.aws.ec2 metrics 90d -o                       # stored data only
```

- Covers instances **stored by an earlier `run`**. Run a full analysis first for new instances.
- `--day` and `--hour` list every hour automatically.
- A day or hour that has not settled yet is rejected, for example
  `no settled hour yet on 2026-11-10 (hours settle 15 min after they end)`.
- A quick analysis that fetches data is recorded as a run with purpose `adhoc`. It does not count
  as the "previous run" of a full analysis.

Hourly listing (`-H`, `-d`, `-t`):

```text
Hourly averages · mysql-proxy · i-03110dcb1d011b816
  2026-11-01 00:00 UTC  CPU 0.9% · Memory 56.8%
  2026-11-01 01:00 UTC  CPU 0.9% · Memory 56.8%
  2026-11-01 02:00 UTC  CPU 0.9% · Memory 56.8%
```

### `history`: stored changes

```bash
python -m app.cloud.aws.ec2 history
python -m app.cloud.aws.ec2 history -r prod-be-fe-001
python -m app.cloud.aws.ec2 history -r i-0022548e58310083f -n 5
```

Every stored change (added, removed, modified) with old → new values, when it was detected and by
which run.

---

## How metrics are collected

| Metric | Namespace | Metric name | Dimension | Statistic | Period | Published by |
|---|---|---|---|---|---|---|
| CPU | `AWS/EC2` | `CPUUtilization` | `InstanceId` | Average | 1 hour | AWS |
| Memory | `EC2/Custom` | `mem_used_percent` | `InstanceId` | Average | 1 hour | CloudWatch agent |

**One datapoint per hour:** the average over that hour. That is 24 per day, 223 for a `30d` run
on 10 November, and 720 for a full 30-day month.

### Fetch only what is missing

Before calling CloudWatch, each run or quick analysis works out, for every instance and metric,
which hours in the window are:

| State | Requested from CloudWatch? |
|---|---|
| **Stored**: a datapoint is in `ec2_metric_datapoints` | never again |
| **Checked**: inside a range in `ec2_metric_fetches`, even if CloudWatch had no data | never again |
| **Missing**: neither | yes, once |

Then:

1. **One request per series:** each instance+metric with missing hours gets one request, from
   its first missing hour to its last. Complete series are **not queried at all**.
2. **Shared calls:** requests with the same time span share `GetMetricData` calls (up to 500
   queries and 100,800 datapoints per call). In a normal repeat run every instance misses the
   same few new hours, so they all fit in one call.
3. **Checked once:** every requested range is recorded as checked. Hours with no CloudWatch
   data (for example no memory agent, or the instance was stopped) are **not retried** on later
   runs.

Example sequence:

| Run | Requested | Calls |
|---|---|---|
| `run` at 07:20 on 10 Nov (first time) | 1 Nov 00:00 → 10 Nov 07:00 | 1 |
| `run` again at 07:40 | nothing (no new settled hour) | **0** |
| `run` at 09:20 | 07:00 → 09:00 only (2 new hours) | 1 |
| `metrics -d 2026-11-05` | nothing (already stored) | **0** |
| `run 90d` | 1 Sep → 1 Nov only (November is stored) | 1 |

Instances without the CloudWatch agent show
`no memory data; the CloudWatch agent is not publishing EC2/Custom mem_used_percent` instead of
failing.

---

## The datapoint report

Printed by every `run` and `metrics`:

```text
Datapoints · 2026-11-01 00:00 UTC → 2026-11-10 07:00 UTC · 223 hours
  Expected          1784 (4 instances × 2 metrics × 223 hours)
  Available         1561 (87.5%)
  Already stored    1553 · not requested again
  Fetched now       8 new datapoints
  No data (checked) 223 hours · CloudWatch had nothing for them
  CloudWatch        1 call · 8 metric queries

Utilization · hourly averages
  mysql-proxy · i-03110dcb1d011b816
    CPU               avg 0.9% · peak 1.4% · 223/223 hours · +2 new
    Memory            avg 56.8% · peak 57.2% · 223/223 hours · +2 new
  prod-opensearch-001 · i-08798f62a79681624
    CPU               avg 7.8% · peak 12.3% · 223/223 hours · +2 new
    Memory            no memory data; the CloudWatch agent is not publishing EC2/Custom mem_used_percent · 0/223 hours (0.0%)
```

| Line | Meaning |
|---|---|
| **Expected** | instances × metrics × hours in the window |
| **Available** | datapoints stored for the window after this run |
| **Already stored** | datapoints that existed before this run; not requested |
| **Fetched now** | new datapoints stored by this run |
| **No data (checked)** | hours checked in CloudWatch that had no value; never retried |
| **Not checked** | hours not checked yet (shown only with `--offline`) |
| **CloudWatch** | API calls and metric queries made: `no calls · nothing missing`, `offline · not called`, or `N calls · M metric queries` |
| **avg** | mean of the hourly averages in the window |
| **peak** | highest hourly average in the window |
| **a/b hours (x%)** | hours with data / hours in the window |
| **+N new** | datapoints for this series fetched by this run |

---

## Where the data is stored

| | |
|---|---|
| Container | `cost-optimizer-analysis-db` (`docker-compose.yml` in the project root) |
| Address | `127.0.0.1:3308` (localhost only) |
| Database | `cost_optimizer` (`ANALYSIS_DB_NAME` in `.env`) |
| On disk | Docker volume `analysis-db-data`, kept by `docker compose down` and deleted by `docker compose down -v` |
| Time zone | All times are **UTC** |

### Tables

| Table | One row per | Contents |
|---|---|---|
| `cloud_accounts` | AWS account | Account ID, first/last seen |
| `analysis_runs` | run | Window, purpose (`production`, `testing`, `adhoc`), status, counts, error |
| `resources` | instance | ID, name, state, first/last seen, last changed, removed |
| `resource_snapshots` | distinct configuration | Full configuration JSON and tracked fields, hash |
| `resource_changes` | detected change | Changed fields (old → new), links to before/after snapshots |
| `resource_tags` | instance tag | Current tags |
| `ec2_instances` | instance | Current specs in typed columns (type, family, size, vCPUs, memory, IPs, ...) |
| `ec2_volumes` | attached volume | Current EBS volumes |
| **`ec2_metric_datapoints`** | **instance, metric, hour** | **The hourly averages** |
| `ec2_metric_fetches` | requested range | Hour ranges already checked in CloudWatch |
| `ec2_metric_collections` | run, instance, metric | What a run expected, requested, fetched and had available |
| `schema_migrations` | migration | Which migration files are applied |

### `ec2_metric_datapoints`

| Column | Example | Meaning |
|---|---|---|
| `resource_ref` | `3` | Link to `resources.id` |
| `instance_id` | `i-03110dcb1d011b816` | AWS instance ID |
| `instance_name` | `mysql-proxy` | Name tag when last collected |
| `metric` | `cpu_utilization` / `memory_utilization` | Which metric |
| `period_start` | `2026-11-01 00:00:00` | Start of the hour (UTC); the hour is `period_start` to +1 h |
| `period_seconds` | `3600` | Always one hour |
| `average` | `0.9` | Average over that hour, in percent |
| `unit` | `Percent` | |
| `run_id` | `6` | Run that last stored this hour |
| `collected_at` | `2026-11-10 01:16:47` | When it was stored |

The primary key is `(resource_ref, metric, period_start)`, so an hour can never be stored twice.

### `ec2_metric_fetches`

| Column | Meaning |
|---|---|
| `instance_id`, `instance_name`, `metric` | Which series |
| `range_start`, `range_end` | Hours requested (`range_end` exclusive, UTC) |
| `requested_hours` | Hours in the range |
| `returned_points` | Datapoints CloudWatch returned (0 = no data, e.g. no memory agent) |
| `run_id`, `fetched_at` | Which run requested it, and when |

### `ec2_metric_collections`

| Column | Meaning |
|---|---|
| `window_start`, `window_end` | The run's window |
| `expected_points` | Hours in the window |
| `requested_points` | Hours requested from CloudWatch by this run |
| `fetched_points` | New datapoints stored by this run |
| `collected_points` | Datapoints available in the window after this run |

---

## Inspecting stored data

### With the app (no CloudWatch calls)

```bash
python -m app.cloud.aws.ec2 metrics -o
python -m app.cloud.aws.ec2 metrics -d 2026-11-01 -o
python -m app.cloud.aws.ec2 metrics -d 2026-11-01 -r mysql-proxy -o
python -m app.db status
```

### With MySQL

Open a MySQL shell inside the container. This uses the credentials stored in the container, so
no password is typed:

```bash
docker exec -it cost-optimizer-analysis-db sh -c 'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"'
```

Every hour of 1 November:

```sql
SELECT instance_name, metric, period_start, ROUND(average, 2) AS avg_percent
FROM ec2_metric_datapoints
WHERE period_start >= '2026-11-01' AND period_start < '2026-11-02'
ORDER BY instance_name, metric, period_start;
```

Hours stored per instance and metric this month:

```sql
SELECT instance_name, metric, COUNT(*) AS hours, MIN(period_start) AS first_hour, MAX(period_start) AS last_hour
FROM ec2_metric_datapoints
WHERE period_start >= '2026-11-01'
GROUP BY instance_name, metric
ORDER BY instance_name, metric;
```

Daily averages and peaks for one instance:

```sql
SELECT DATE(period_start) AS day, metric,
       ROUND(AVG(average), 2) AS daily_avg, ROUND(MAX(average), 2) AS peak_hour
FROM ec2_metric_datapoints
WHERE instance_id = 'i-03110dcb1d011b816' AND period_start >= '2026-11-01'
GROUP BY day, metric
ORDER BY day, metric;
```

What each run cost in CloudWatch:

```sql
SELECT a.id AS run, a.window_key, a.purpose, a.started_at,
       COUNT(f.id) AS ranges_requested, COALESCE(SUM(f.requested_hours), 0) AS hours_requested,
       COALESCE(SUM(f.returned_points), 0) AS datapoints_returned
FROM analysis_runs a
LEFT JOIN ec2_metric_fetches f ON f.run_id = a.id
GROUP BY a.id
ORDER BY a.id;
```

Series with no CloudWatch data (for example a missing memory agent):

```sql
SELECT instance_name, metric, SUM(requested_hours) AS hours_checked, SUM(returned_points) AS datapoints
FROM ec2_metric_fetches
GROUP BY instance_name, metric
HAVING datapoints = 0;
```

---

## Files in this folder

| File | Responsibility |
|---|---|
| `__main__.py` | Command line: `list`, `config`, `run`, `metrics`, `history` and their options |
| `__init__.py` | Public API for other modules (`run_analysis`, `analyze`, `list_instances`, ...) |
| `instances.py` | `describe_instances()` (the single EC2 call), `list_instances()`, list output |
| `configuration.py` | `get_configurations()`: full specs, instance types and EBS volumes |
| `changes.py` | `InstanceSnapshot`, `compare()`: added, removed and modified instances |
| `metrics.py` | CloudWatch metric definitions and `fetch()` (hour planning is shared: `app/cloud/timeseries.py`) |
| `utilization.py` | Fill missing hours, the datapoint report, quick analysis (`analyze()`, day/hour selection) |
| `store.py` | Writes and reads instances, snapshots, changes, datapoints, checked ranges, run summaries |
| `analysis.py` | `run_analysis()`: the full analysis, readiness, history |
| `evidence.py` | Evidence document (EC2 source and configuration) and validation |
| `pipeline.py` | The nine-step evidence pipeline |
| `pricing.py` | AWS Price List API on-demand prices |
| `README.md` | This document |

Shared code used here (not duplicated, also used by `app/cloud/azure/vm`):
`app/cloud/{timeseries,windows,cumulative,utilization,evidence,evidence_output,pricing,backoff,display}.py`
and `app/db/metrics.py`. Also:

| File | Used for |
|---|---|
| `app/auth/aws_auth.py` | `get_client()` / `get_session()`: the only way to connect to AWS |
| `app/auth/mysql_auth.py` | Database connections (`ANALYSIS` profile for writes; the company DB is read-only) |
| `app/cloud/aws/errors.py` | `aws_errors()`: boto errors turned into messages with IAM hints |
| `app/cloud/display.py` | Rich output: headers, sections, label/value lines, `format_time()`, `plural()` |
| `app/db/database.py` | `session()` and automatic schema migrations |
| `app/db/resources.py` | Generic storage: accounts, runs, resources, snapshots, changes, tags |

---

## Database migrations

The schema lives in `app/db/migrations/`. Pending files are applied automatically by the next
`run` or `metrics`.

| File | Adds |
|---|---|
| `0001_initial_schema.sql` | Accounts, runs, resources, snapshots, changes, tags, `ec2_instances`, `ec2_volumes` |
| `0002_ec2_metrics.sql` | `ec2_metric_datapoints`, `ec2_metric_collections` |
| `0003_ec2_metrics_instance_columns.sql` | `instance_id`, `instance_name` on both metrics tables |
| `0004_ec2_metric_fetches.sql` | `ec2_metric_fetches`; `requested_points`, `fetched_points` on collections |
| `0005_ec2_metric_cumulative.sql` | `ec2_metric_cumulative`: cumulative statistics per configuration version |
| `0006_azure_vm_tables.sql` | `azure_vms`, `azure_vm_disks`, `azure_vm_metric_*`; wider `analysis_runs.window_key` |

```bash
python -m app.db status     # applied / pending migrations and row counts
python -m app.db migrate    # apply pending migrations now
```

To store new kinds of data, add the next numbered file, for example `0007_<name>.sql`. Never
edit a migration that has already been applied; the app refuses to run if one changes.

---

## Required IAM permissions (read-only)

| Permission | Used by |
|---|---|
| `sts:GetCallerIdentity` | every run (account ID) |
| `ec2:DescribeInstances` | `list`, `config`, `run` |
| `ec2:DescribeInstanceTypes` | `config`, `run` (vCPUs, memory) |
| `ec2:DescribeVolumes` | `config`, `run` (EBS details) |
| `cloudwatch:GetMetricData` | `run`, `metrics` (only when hours are missing) |

A missing permission is reported with the exact action to grant.

Related checks:

```bash
python -m app.auth aws            # AWS credentials from .env
python -m app.auth analysis-db    # local analysis database
docker compose up -d              # start the local analysis database
```

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Failure (credentials, permissions, database, AWS error); message and hints are printed |
| `2` | Invalid command or option (e.g. bad `--day`, unsettled `--hour`, WINDOW together with `--day`) |

---

## Known limitations

- **Gap spans:** an instance with a gap in the middle of its stored hours gets one request from
  its first missing hour to its last. The hours in between are requested again but stored only
  once. This keeps the number of calls low; CloudWatch bills per metric queried, not per datapoint.
- **No retry for empty hours:** an hour CloudWatch had no data for is never retried, even if the
  agent is installed later. CloudWatch does not backfill old hours, so retrying would not help.
- **Stored instances only:** `metrics` only covers instances stored by a `run`. Run a full
  analysis to pick up new instances.
- **UTC everywhere:** `--day today` means today in UTC.
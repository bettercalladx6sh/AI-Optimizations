# Cloudpositive AI Engine

Evidence-based rightsizing for **AWS EC2 instances** and **Azure virtual machines**.

The app reads your cloud accounts (read-only), collects hourly CPU and memory utilization,
keeps cumulative statistics per configuration in a local database, builds a validated JSON
**evidence** document, and then turns that evidence into priced rightsizing
**recommendations**: rule-based sizing, live list prices, and an AI explanation (OpenAI).

```
cloud account ──read-only──▶ analysis (local DB) ──▶ evidence JSON ──▶ AI engine ──▶ recommendations JSON
```

---

## Contents

1. [Ground rules](#1-ground-rules)
2. [Requirements](#2-requirements)
3. [Getting started from scratch](#3-getting-started-from-scratch)
4. [Configuration](#4-configuration)
5. [Entry points](#5-entry-points)
6. [Workflow](#6-workflow)
7. [Command reference](#7-command-reference)
8. [Analysis windows](#8-analysis-windows)
9. [Output files](#9-output-files)
10. [Project structure](#10-project-structure)
11. [Database](#11-database)
12. [Everyday recipes](#12-everyday-recipes)
13. [Troubleshooting and exit codes](#13-troubleshooting-and-exit-codes)
14. [Further documentation](#14-further-documentation)

---

## 1. Ground rules

These hold for every command in the app:

- **Clouds are only read.** AWS and Azure are called with read-only APIs only; nothing in
  an account is created, changed or deleted.
- **The company database is only read.** The company UAT database (`DB_*`) is opened in
  read-only sessions. Everything the app writes goes to the **local analysis database**
  (`ANALYSIS_DB_*`, started with docker compose).
- **Evidence is not stored in the database.** It is built in memory on every run, validated
  against a JSON Schema, and written to one JSON file per account, region and window. Each
  new run of that window **replaces the file in place**.
- **Metrics are fetched once.** Every hour of every metric is requested from the cloud at
  most once; a repeat run only asks for hours that are still missing.
- **Nothing is guessed.** A size is recommended only when the evidence is sufficient, a
  price only when the cloud returned exactly one, and missing data is never treated as zero.

---

## 2. Requirements

| Requirement | Why |
|---|---|
| Python 3 (developed and tested on 3.14) | The app |
| Docker with docker compose | The local analysis database (MySQL 8.4) |
| AWS access key (read-only IAM) | EC2 analysis |
| Azure identity (Reader, Monitoring Reader, Log Analytics Reader) | Azure VM analysis |
| OpenAI API key | AI reasoning (optional: `--no-ai` works without it) |

Python packages are listed in [`requirements.txt`](requirements.txt): `python-dotenv`,
`rich`, `boto3`, `azure-identity`, `azure-mgmt-resource`, `azure-mgmt-resource-subscriptions`,
`requests`, `PyMySQL[rsa]`, `opensearch-py`, `urllib3`, `jsonschema`, `openai`.

---

## 3. Getting started from scratch

Run everything from the project root.

**Step 1: create the virtual environment and install the packages**

```bash
python3 -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install -r requirements.txt
```

**Step 2: create `.env`** in the project root with the variables in
[Configuration](#4-configuration). `.env` is git-ignored; never commit it.

**Step 3: start the local analysis database**

```bash
docker compose up -d
```

**Step 4: check every connection**

```bash
python -m app.auth
```

Each provider prints a ✔ or ✘ with hints. Check one at a time with `python -m app.auth aws`,
`azure`, `mysql`, `analysis-db` or `opensearch`.

**Step 5: create the database tables** (optional; the first analysis run does this too)

```bash
python -m app.db migrate
```

**Step 6: run the engine**

AWS EC2, last 7 days (testing window), evidence then AI recommendations:

```bash
python -m app.reasoning 7d
```

Azure VMs, same window:

```bash
python -m app.reasoning --cloud azure 7d
```

Without an OpenAI key, add `--no-ai` for rule-based decisions only. The results are printed
and written to `recommendations/…` (see [Output files](#9-output-files)).

---

## 4. Configuration

### `.env` (secrets and connections)

| Variable | Used for | Required |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | AWS access key | for AWS |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | for AWS |
| `AWS_DEFAULT_REGION` | Region analysed by default, e.g. `us-east-1` | for AWS |
| `AZURE_SUBSCRIPTION_ID` | Subscription analysed | for Azure |
| `AZURE_TENANT_ID` | Home tenant of the app registration / managed identity | for Azure |
| `AZURE_TARGET_TENANT_ID` | Tenant that holds the subscription (cross-tenant); defaults to `AZURE_TENANT_ID` | optional |
| `AZURE_APP_CLIENT_ID` | Multi-tenant app registration trusted by the managed identity | for federated auth |
| `AZURE_UAMI_CLIENT_ID` | User-assigned managed identity | for federated auth |
| `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Service principal (alternative to federated auth) | optional |
| `AZURE_LOG_ANALYTICS_WORKSPACE_ID` | Workspace with VM insights; memory then comes from Log Analytics | recommended |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_SSL_CA` | Company UAT database (read-only) | for `app.auth mysql` |
| `OPENSEARCH_URL`, `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_USE_SSL`, `OPENSEARCH_VERIFY_CERTS` | OpenSearch connection | for `app.auth opensearch` |
| `ANALYSIS_DB_HOST`, `ANALYSIS_DB_PORT`, `ANALYSIS_DB_USER`, `ANALYSIS_DB_PASSWORD`, `ANALYSIS_DB_NAME`, `ANALYSIS_DB_ROOT_PASSWORD` | Local analysis database (docker compose; port defaults to 3308) | yes |
| `OPENAI_API_KEY` | AI reasoning | unless `--no-ai` |
| `OPENAI_MODEL` | OpenAI model (default in `app/reasoning/config.py`) | optional |

**Azure authentication order** (the first that works is used for the whole run):

1. **Federated managed identity**: with `AZURE_UAMI_CLIENT_ID` and `AZURE_APP_CLIENT_ID`, the
   managed identity signs in as the app registration in the target tenant, with no secret.
2. **Service principal**: `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET`.
3. **Azure CLI**: an `az login` session.
4. **Managed identity** of the host itself.

**Permissions needed:**

| Cloud | Permission | For |
|---|---|---|
| AWS | `ec2:DescribeInstances`, `ec2:DescribeVolumes`, `ec2:DescribeInstanceTypes`, `cloudwatch:GetMetricData`, `pricing:GetProducts`, `sts:GetCallerIdentity` | discovery, metrics, prices (full list in the EC2 guide) |
| Azure | **Reader** on the subscription | VMs, disks, VM sizes |
| Azure | **Monitoring Reader** on the subscription | CPU and memory platform metrics |
| Azure | **Log Analytics Reader** on the workspace | memory from VM insights |

### `app/config.py` (analysis settings)

| Setting | Default | Meaning |
|---|---|---|
| `ANALYSIS_WINDOWS` | `1h 1d 3d 7d 30d 90d` | Windows selectable on the command line |
| `DEFAULT_WINDOW` | `30d` | Window used when none is given |
| `SETTLE_DELAY_MINUTES` | 15 | An hour's average is final this long after the hour ends |
| `MIN_COVERAGE_PERCENT` | 90 | Each metric must cover this share of the window hours |
| `MIN_EVIDENCE_HOURS` | 1 | …and at least this many hours |
| `LIMITED_HISTORY_HOURS` | 24 | Flag metrics with fewer hours than this |
| `CPU_SAFETY_FACTOR` | 1.25 | Required vCPU = p95 CPU × factor |
| `MEMORY_SAFETY_FACTOR` | 1.25 | Required memory = p95 memory × factor |

---

## 5. Entry points

Every entry point is a Python module run with `python -m` from the project root.

| Entry point | File | What it does |
|---|---|---|
| `python -m app.reasoning` | `app/reasoning/__main__.py` | **Main entry point.** The whole flow: evidence pipeline, then AI rightsizing recommendations (EC2 or Azure) |
| `python -m app.cloud.aws.ec2` | `app/cloud/aws/ec2/__main__.py` | EC2: list, configuration, analysis, quick metrics, history, evidence, validate |
| `python -m app.cloud.aws.ec2.pipeline` | `app/cloud/aws/ec2/pipeline.py` | EC2 evidence pipeline only |
| `python -m app.cloud.aws.ec2.pricing` | `app/cloud/aws/ec2/pricing.py` | EC2 on-demand prices |
| `python -m app.cloud.azure.vm` | `app/cloud/azure/vm/__main__.py` | Azure VMs: list, configuration, analysis, evidence, validate |
| `python -m app.cloud.azure.vm.pipeline` | `app/cloud/azure/vm/pipeline.py` | Azure evidence pipeline only |
| `python -m app.cloud.azure.vm.pricing` | `app/cloud/azure/vm/pricing.py` | Azure pay-as-you-go prices |
| `python -m app.auth` | `app/auth/__main__.py` | Connection checks: AWS, Azure, MySQL, analysis DB, OpenSearch |
| `python -m app.db` | `app/db/__main__.py` | Analysis database: migration status and row counts, apply migrations |

Every command accepts `-h` / `--help`.

---

## 6. Workflow

`python -m app.reasoning` runs **15 steps**: the 9-step evidence pipeline of the chosen
cloud, then the 6 reasoning steps. Each step prints `✔ n/15 Title · detail` as it finishes,
or `✘` with a fix hint when it fails.

### Part 1: evidence pipeline (per cloud)

| # | Step | EC2 | Azure VMs |
|---|---|---|---|
| 1 | Connect | AWS STS identity, region | Azure credential, subscription, location |
| 2 | Find the instances | `DescribeInstances` | VM list + power states |
| 3 | Details and configurations | instance types, EBS volumes | VM size specs (SKUs), managed disks |
| 4 | Configuration changes | compared with the stored baseline (first run stores it) | same |
| 5 | Collect metrics (missing hours only) | CloudWatch: `CPUUtilization`, memory `EC2/Custom mem_used_percent` | Azure Monitor `Percentage CPU`; memory from Log Analytics VM insights or `Available Memory Bytes` |
| 6 | Store and cumulative averages | local DB; statistics per configuration version | same |
| 7 | Prepare evidence | built in memory | same |
| 8 | Validate evidence | JSON Schema + cross-field rules | same |
| 9 | Write evidence file | `evidence/aws/ec2/…` (replaced) | `evidence/azure/vm/…` (replaced) |

**Readiness:** an instance is evaluated only when its current configuration covers the whole
window. After a launch, resize or other significant change, it waits until the window no
longer holds hours from before the change. The output says until when.

### Part 2: AI engine (same for both clouds)

| # | Step | What happens |
|---|---|---|
| 10 | Check evidence sufficiency | Gate without AI: running, ready, CPU and memory coverage ≥ thresholds. Otherwise `INSUFFICIENT_DATA` with what is missing and when to run again |
| 11 | Validate candidate sizes | Required capacity = p95 × safety factor; every size of the instance's own family is checked |
| 12 | Fetch prices | AWS Price List API (on-demand) or Azure Retail Prices API (pay-as-you-go) |
| 13 | AI reasoning | OpenAI explains the rule-based decision and may only choose it or a more cautious one (`KEEP_CURRENT`, `INVESTIGATE`) |
| 14 | Validate recommendations | JSON Schema + policy checks (a recommended size must be a validated safe candidate) |
| 15 | Write recommendations file | `recommendations/<cloud>/<service>/…` (replaced) |

**Decisions:** `DOWNSIZE_CANDIDATE`, `UPSCALE_CANDIDATE`, `KEEP_CURRENT`, `INVESTIGATE`,
`INSUFFICIENT_DATA`. Only the first two carry a recommended size.

### How the modules connect

```
app/reasoning/__main__.py
  └─ app/reasoning/engine.py ── providers.py picks the cloud adapter
       ├─ evidence pipeline:  app/cloud/aws/ec2/pipeline.py  or  app/cloud/azure/vm/pipeline.py
       │     ├─ analysis.py      steps 1-6 (discovery, changes, metrics, storage)
       │     ├─ evidence.py      step 7  (+ shared app/cloud/evidence.py)
       │     └─ app/cloud/evidence_output.py   steps 8-9
       ├─ eligibility.py   step 10
       ├─ sizing.py        step 11
       ├─ <cloud>/pricing.py   step 12
       ├─ ai.py            step 13
       └─ render.py        terminal output
```

---

## 7. Command reference

### 7.1 Connections

```bash
python -m app.auth                 # check every connection
python -m app.auth aws             # AWS only
python -m app.auth azure           # Azure only (tenant, identity, subscription, workspace)
python -m app.auth mysql           # company UAT database (read-only)
python -m app.auth analysis-db     # local analysis database
python -m app.auth opensearch      # OpenSearch
```

### 7.2 Local analysis database

```bash
docker compose up -d               # start
docker compose down                # stop (data kept)
docker compose down -v             # stop and DELETE all analysis data
python -m app.db status            # migrations applied/pending, row counts per table
python -m app.db migrate           # apply pending migrations now
```

### 7.3 AI engine (recommended entry point)

```bash
python -m app.reasoning                             # EC2, default window (30d), AI
python -m app.reasoning 7d                          # EC2, testing window
python -m app.reasoning 7d --region eu-west-1       # EC2, another region
python -m app.reasoning --cloud azure               # Azure VMs, every location
python -m app.reasoning --cloud azure 7d --location eastus
python -m app.reasoning 7d --no-ai                  # rules only, no OpenAI call
python -m app.reasoning -e evidence/aws/ec2/<account>/<region>/7d.json   # reuse evidence (any cloud)
python -m app.reasoning 7d -o rec.json -p           # custom output file, also print JSON
```

| Option | Meaning |
|---|---|
| `WINDOW` | `1h`, `1d`, `3d`, `7d`, `30d` (default), `90d` |
| `--cloud aws\|azure` | Cloud to analyse when the pipeline runs (default `aws`) |
| `--region R` | AWS region (default `AWS_DEFAULT_REGION`) |
| `--location L` | Azure location (default: every location) |
| `-e FILE` | Use an existing evidence file instead of running the pipeline |
| `--no-ai` | Rule-based decisions only |
| `-o FILE` | Recommendations file to write |
| `-p` | Also print the recommendations JSON |

### 7.4 AWS EC2

```bash
python -m app.cloud.aws.ec2                          # list instances (default command)
python -m app.cloud.aws.ec2 list --state running     # only running
python -m app.cloud.aws.ec2 config                   # full configuration of every instance
python -m app.cloud.aws.ec2 config -r i-0abc123      # one instance by ID or Name tag
python -m app.cloud.aws.ec2 run                      # analysis, month to date
python -m app.cloud.aws.ec2 run 7d                   # analysis, rolling 7 days
python -m app.cloud.aws.ec2 metrics                  # quick CPU/memory, default window
python -m app.cloud.aws.ec2 metrics -d yesterday     # one UTC day, every hour
python -m app.cloud.aws.ec2 metrics -t "2026-11-10 06"   # one UTC hour
python -m app.cloud.aws.ec2 metrics 7d -r api-01     # one instance
python -m app.cloud.aws.ec2 metrics -o               # stored data only, no CloudWatch calls
python -m app.cloud.aws.ec2 history -r i-0abc123     # stored configuration changes
python -m app.cloud.aws.ec2 evidence 7d              # 9-step evidence pipeline
python -m app.cloud.aws.ec2 validate FILE.json       # check an evidence file
python -m app.cloud.aws.ec2.pricing t3a.medium t3a.small               # on-demand prices
python -m app.cloud.aws.ec2.pricing m5.large --os Windows --region eu-west-1
```

Every EC2 command except `validate` accepts `--region`.

### 7.5 Azure VMs

```bash
python -m app.cloud.azure.vm                         # list VMs (default command)
python -m app.cloud.azure.vm list --location eastus  # one location
python -m app.cloud.azure.vm config                  # full configuration of every VM
python -m app.cloud.azure.vm config -r web-01        # one VM by name, vmId or resource ID
python -m app.cloud.azure.vm run                     # analysis, month to date
python -m app.cloud.azure.vm run 7d                  # analysis, rolling 7 days
python -m app.cloud.azure.vm evidence 7d             # 9-step evidence pipeline
python -m app.cloud.azure.vm validate FILE.json      # check an evidence file
python -m app.cloud.azure.vm.pricing Standard_D2s_v5 Standard_D4s_v5 --location eastus
python -m app.cloud.azure.vm.pricing Standard_E4s_v5 --location westeurope --os Windows
```

Every Azure command except `validate` accepts `--location` (default: every location).

### 7.6 Evidence pipeline options (EC2 and Azure)

| Option | Meaning |
|---|---|
| `WINDOW` | Analysis window (default `30d`) |
| `--region` / `--location` | Scope |
| `-o FILE` | Evidence file to write instead of the default path |
| `--no-file` | Do not write a file (combine with `-p`) |
| `-p` | Also print the evidence JSON |

---

## 8. Analysis windows

| Key | Purpose | Covers |
|---|---|---|
| `1h` | testing | the last settled hour |
| `1d` | testing | rolling 24 hours |
| `3d` | testing | rolling 3 days |
| `7d` | testing | rolling 7 days |
| `30d` | production (default) | **month to date**: from the 1st of this month 00:00 UTC |
| `90d` | production | **last 3 calendar months to date**: from the 1st of the month two months ago |

Every window ends at the last settled hour (15 minutes after the hour). Labels and evidence
use the real span at run time; on 10 November, `30d` covers 1-10 November. Edit
`app/config.py` to add or change windows.

---

## 9. Output files

Both folders are git-ignored because they hold account details. Each file is replaced in
place on the next run of the same window, and only after the new content passed validation.

| Path | Written by | Contains |
|---|---|---|
| `evidence/aws/ec2/<account>/<region>/<window>.json` | EC2 pipeline | Per instance: configuration, changes, readiness, CPU/memory statistics, data-quality issues |
| `evidence/azure/vm/<subscription>/<location\|all>/<window>.json` | Azure pipeline | Same structure for Azure VMs |
| `recommendations/aws/ec2/<account>/<region>/<window>.json` | AI engine | Per instance: eligibility, capacity, candidates, decision, recommended size, prices, savings, explanation |
| `recommendations/azure/vm/<subscription>/<location\|all>/<window>.json` | AI engine | Same for Azure VMs |

Their formats are defined by the JSON Schemas listed in
[Project structure](#10-project-structure).

---

## 10. Project structure

```
AI-Optimizations/
├── README.md                    this guide
├── requirements.txt             Python dependencies
├── docker-compose.yml           local analysis database (MySQL 8.4, 127.0.0.1:3308)
├── .env                         secrets and connections (git-ignored, create it yourself)
├── .gitignore
├── evidence/                    generated evidence JSON (git-ignored)
├── recommendations/             generated recommendations JSON (git-ignored)
└── app/
    ├── config.py                analysis windows, thresholds, safety factors
    ├── auth/                    connections to clouds, databases, OpenSearch
    ├── db/                      local analysis database: sessions, migrations, storage
    ├── cloud/                   provider-neutral analysis code
    │   ├── aws/                 AWS helpers
    │   │   └── ec2/             EC2 discovery, analysis, evidence, pricing
    │   └── azure/               Azure REST client
    │       └── vm/              Azure VM discovery, analysis, evidence, pricing
    └── reasoning/               AI engine: gate, sizing, pricing, AI, recommendations
```

### Root

| File | Purpose |
|---|---|
| `requirements.txt` | Python packages; install with `pip install -r requirements.txt` |
| `docker-compose.yml` | Starts the local analysis database from the `ANALYSIS_DB_*` values in `.env`; data lives in the `analysis-db-data` volume |
| `.env` | Credentials and connection settings (never committed) |
| `.gitignore` | Ignores `.env`, `.venv`, caches, `evidence/` and `recommendations/` |

### `app/`

| File | Purpose |
|---|---|
| `config.py` | Analysis windows, default window, settle delay, evidence thresholds, CPU/memory safety factors |

### `app/auth/`: connections

| File | Purpose |
|---|---|
| `__main__.py` | `python -m app.auth [aws\|azure\|mysql\|analysis-db\|opensearch]`: connection checks |
| `__init__.py` | Exposes the auth modules and `AuthError` |
| `common.py` | `.env` reading, the shared console, status/hint/warning output, `AuthError` base class |
| `aws_auth.py` | The single way to connect to AWS: session, clients, identity check |
| `azure_auth.py` | The single way to connect to Azure: credential chain (federated managed identity, service principal, CLI, managed identity), cached tokens, subscription check |
| `mysql_auth.py` | Database connections: `SOURCE` (company DB, read-only sessions) and `ANALYSIS` (local DB, read-write) |
| `opensearch_auth.py` | OpenSearch connection and check |

### `app/db/`: local analysis database

| File | Purpose |
|---|---|
| `__main__.py` | `python -m app.db status \| migrate` |
| `__init__.py` | Exposes `session`, `migrate`, `available_migrations` |
| `database.py` | Sessions to the analysis DB; applies pending migrations automatically and refuses changed ones |
| `resources.py` | Generic storage: accounts, analysis runs, resources, snapshots, changes, tags |
| `metrics.py` | `MetricTables`: hourly datapoints, checked hour ranges, per-run collections and cumulative summaries, one table set per service (`ec2_*`, `azure_vm_*`) |
| `migrations/0001_initial_schema.sql` | Accounts, runs, resources, snapshots, changes, tags, `ec2_instances`, `ec2_volumes` |
| `migrations/0002_ec2_metrics.sql` | `ec2_metric_datapoints`, `ec2_metric_collections` |
| `migrations/0003_ec2_metrics_instance_columns.sql` | Instance ID and name on the metric tables |
| `migrations/0004_ec2_metric_fetches.sql` | `ec2_metric_fetches` (hours already requested) |
| `migrations/0005_ec2_metric_cumulative.sql` | `ec2_metric_cumulative` (statistics per configuration version) |
| `migrations/0006_azure_vm_tables.sql` | `azure_vms`, `azure_vm_disks`, `azure_vm_metric_*`; wider window key |

### `app/cloud/`: shared by every cloud

| File | Purpose |
|---|---|
| `timeseries.py` | Metric definitions, settled hours, coverage, which hours still need fetching |
| `windows.py` | Turns `config.py` windows into exact start/end hours; evaluation readiness |
| `cumulative.py` | Cumulative statistics: average, min, max, p50/p95/p99, std deviation, trend, volatility, gaps |
| `utilization.py` | Stores fetched datapoints, the datapoint report, cumulative summaries and their output |
| `evidence.py` | Provider-neutral evidence blocks (evaluation, metrics, data quality) and cross-field checks |
| `evidence_output.py` | Pipeline steps 7-9: summarize, validate, write the evidence file; validate an existing file |
| `schema.py` | JSON Schema (draft 2020-12) validation with JSONPath-style issue locations |
| `pricing.py` | Price records (`InstancePrice`, `PricingBasis`), 730-hour month |
| `backoff.py` | Exponential backoff with jitter for throttled or unavailable APIs |
| `display.py` | Terminal output: headers, aligned label/value lines, times, the step printer |
| `files.py` | Atomic JSON writes (replace in place), project root, display paths |

### `app/cloud/aws/`: AWS helpers

| File | Purpose |
|---|---|
| `backoff.py` | Which AWS errors are retryable; `with_backoff()` |
| `errors.py` | Turns boto errors into messages with IAM permission hints |

### `app/cloud/aws/ec2/`: EC2

| File | Purpose |
|---|---|
| `__main__.py` | EC2 command line (`list`, `config`, `run`, `metrics`, `history`, `evidence`, `validate`) |
| `__init__.py` | Public API for other modules |
| `instances.py` | `describe_instances()` and the instance list |
| `configuration.py` | Full specs: instance types, EBS volumes, network, tags |
| `changes.py` | Tracked fields, snapshots, change detection |
| `metrics.py` | CloudWatch metric definitions and fetching |
| `utilization.py` | Quick metrics analysis (window, day, hour selections) |
| `store.py` | EC2 rows in the analysis DB (`ec2_instances`, `ec2_volumes`, `ec2_metric_*`) |
| `analysis.py` | Pipeline steps 1-6, readiness, change history |
| `evidence.py` | EC2 evidence document and validation |
| `pipeline.py` | The 9-step evidence pipeline |
| `pricing.py` | AWS Price List API on-demand prices |
| `schemas/ec2_evidence.schema.json` | JSON Schema of EC2 evidence |
| `README.md` | Detailed EC2 guide |

### `app/cloud/azure/`: Azure

| File | Purpose |
|---|---|
| `rest.py` | Read-only REST client: Resource Manager (pinned api-versions, paging), Log Analytics queries, public Retail Prices API; backoff and permission hints |

### `app/cloud/azure/vm/`: Azure VMs

| File | Purpose |
|---|---|
| `__main__.py` | Azure VM command line (`list`, `config`, `run`, `evidence`, `validate`) |
| `inventory.py` | VM listing, power states, managed disks, configuration output |
| `skus.py` | VM size specs and family members per location (Compute Resource SKUs) |
| `changes.py` | Tracked fields, snapshots, change detection |
| `metrics.py` | Azure Monitor and Log Analytics metric definitions and fetching |
| `store.py` | Azure rows in the analysis DB (`azure_vms`, `azure_vm_disks`, `azure_vm_metric_*`) |
| `analysis.py` | Pipeline steps 1-6, readiness |
| `evidence.py` | Azure VM evidence document and validation |
| `pipeline.py` | The 9-step evidence pipeline |
| `pricing.py` | Azure Retail Prices API pay-as-you-go prices |
| `schemas/azure_vm_evidence.schema.json` | JSON Schema of Azure VM evidence |
| `README.md` | Detailed Azure VM guide |

### `app/reasoning/`: AI engine

| File | Purpose |
|---|---|
| `__main__.py` | `python -m app.reasoning`: the main entry point |
| `engine.py` | Runs the reasoning steps, builds and validates the recommendations report, writes it |
| `providers.py` | One adapter per cloud: pipeline, validator, candidate sizes, prices, AI details, schema |
| `eligibility.py` | Evidence gate: decides without AI whether an instance can be evaluated |
| `sizing.py` | Required capacity, candidate validation, rule-based decision, price refinement |
| `ai.py` | OpenAI call with a strict response schema; retries invalid answers |
| `config.py` | OpenAI key and model, AI retry limit |
| `render.py` | Terminal output of the recommendations |
| `schemas/ec2_recommendations.schema.json` | JSON Schema of EC2 recommendations |
| `schemas/azure_vm_recommendations.schema.json` | JSON Schema of Azure VM recommendations |

---

## 11. Database

The local analysis database runs in docker (`cost-optimizer-analysis-db`, port
`127.0.0.1:3308` by default). Migrations in `app/db/migrations/` are applied automatically by
the first run that opens a session, or with `python -m app.db migrate`.

What is stored:

- **Accounts and runs:** every analysis run with its window and result.
- **Resources:** every instance or VM, its configuration snapshots, tags and detected changes.
- **Typed details:** `ec2_instances`, `ec2_volumes`, `azure_vms`, `azure_vm_disks`.
- **Metrics** (`ec2_metric_*`, `azure_vm_metric_*`): hourly averages, which hours were
  already checked at the cloud, per-run collections, and cumulative statistics per
  configuration version.

Evidence and recommendations are **not** stored in the database, only in their JSON files.

To add new kinds of data, add the next numbered migration (`0007_<name>.sql`). Never edit a
migration that was already applied; the app refuses to run if one changes.

---

## 12. Everyday recipes

**Production run (month to date), both clouds:**

```bash
python -m app.reasoning
```
```bash
python -m app.reasoning --cloud azure
```

**Quick test without AI cost:**

```bash
python -m app.reasoning 1d --no-ai
```

**Prepare evidence now, reason later:**

```bash
python -m app.cloud.azure.vm evidence 7d
```
```bash
python -m app.reasoning -e evidence/azure/vm/<subscription>/all/7d.json
```

**Inspect one machine:**

```bash
python -m app.cloud.aws.ec2 config -r api-01
```
```bash
python -m app.cloud.azure.vm config -r web-01
```

**See what changed on an EC2 instance:**

```bash
python -m app.cloud.aws.ec2 history -r i-0abc123
```

**Check the database:**

```bash
python -m app.db status
```

---

## 13. Troubleshooting and exit codes

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | A step failed (connection, permission, validation); the message and hints say why |
| `2` | Invalid command-line arguments, or an unknown provider for `python -m app.auth` |

| Symptom | Fix |
|---|---|
| `✘ … is not set in .env` | Add the variable named in the message to `.env` |
| `✘ 1/… Connect …` | Run `python -m app.auth aws` or `azure` and follow the hints |
| `Azure denied … (403)` / `AccessDenied` | Grant the role or IAM permission named in the hint |
| Analysis DB connection refused | `docker compose up -d`, then `python -m app.auth analysis-db` |
| Many instances `INSUFFICIENT_DATA` | Expected right after setup or a change; the output says what is missing and when to run again |
| Memory has no datapoints (EC2) | Install the CloudWatch agent publishing `EC2/Custom mem_used_percent` |
| Memory has no datapoints (Azure) | Set `AZURE_LOG_ANALYTICS_WORKSPACE_ID` with VM insights enabled, or make sure `Available Memory Bytes` is published |
| `OPENAI_API_KEY is not set` | Add it to `.env`, or run with `--no-ai` |
| Pricing "not available" | Shown with the reason (Spot, dedicated host, licensed image, no unambiguous price); no cost is guessed |

---

## 14. Further documentation

| Guide | Covers |
|---|---|
| [`app/cloud/aws/ec2/README.md`](app/cloud/aws/ec2/README.md) | EC2 in depth: every option, metric collection, the datapoint report, stored data, IAM permissions, limitations |
| [`app/cloud/azure/vm/README.md`](app/cloud/azure/vm/README.md) | Azure VMs in depth: authentication, roles, metrics, VM identity, pricing rules |

Every Python module also starts with a docstring explaining its role.

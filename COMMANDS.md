# Command reference

Every command of the Cloudpositive AI Engine in one place. Run them from the project root
with the virtual environment active. For what each module does and how the parts fit
together, see [README.md](README.md).

**Read-only rule:** the company database (`DB_*`) and OpenSearch (`OPENSEARCH_*`) are only
ever read. Writes go to the local analysis database (`ANALYSIS_DB_*`, docker compose) and to
files in `evidence/`, `recommendations/` and `opensearch_results/`.

## Index

| # | Section | Commands |
|---|---|---|
| 1 | [Setup](#1-setup) | venv, packages, `.env`, docker |
| 2 | [Connection checks](#2-connection-checks) | `app.auth` |
| 3 | [Local analysis database](#3-local-analysis-database) | `docker compose`, `app.db` |
| 4 | [AI analysis (recommended entry point)](#4-ai-analysis-recommended-entry-point) | `app.reasoning` |
| 5 | [AWS EC2](#5-aws-ec2) | `app.cloud.aws.ec2`, `app.cloud.aws.ec2.pricing` |
| 6 | [Azure VMs](#6-azure-vms) | `app.cloud.azure.vm`, `app.cloud.azure.vm.pricing` |
| 7 | [Company database queries](#7-company-database-queries) | `app.server.mysql.*` |
| 8 | [OpenSearch super index](#8-opensearch-super-index) | `app.server.opensearch.*` |
| 9 | [Analysis windows](#9-analysis-windows) | `1h` … `90d` |
| 10 | [Output files](#10-output-files) | where every command writes |
| 11 | [Everyday recipes](#11-everyday-recipes) | common sequences |
| 12 | [Exit codes and quick fixes](#12-exit-codes-and-quick-fixes) | troubleshooting |

**Quick start (copy in order):**

```bash
source .venv/bin/activate
docker compose up -d
python -m app.auth
python -m app.reasoning 7d                  # AWS EC2: evidence + AI recommendations
python -m app.reasoning --cloud azure 7d    # Azure VMs: evidence + AI recommendations
```

Every command accepts `-h` / `--help` for its full option list.

---

## 1. Setup

```bash
python3 -m venv .venv                # create the virtual environment (once)
source .venv/bin/activate            # activate it (every new terminal)
pip install -r requirements.txt      # install the packages
```

Create `.env` in the project root (git-ignored, never commit it). Variables by area:

| Area | Variables |
|---|---|
| AWS | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` |
| Azure | `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`, `AZURE_TARGET_TENANT_ID`, `AZURE_APP_CLIENT_ID`, `AZURE_UAMI_CLIENT_ID`, `AZURE_LOG_ANALYTICS_WORKSPACE_ID` (or `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` for a service principal) |
| Company database (read-only) | `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_SSL_CA` |
| Local analysis database | `ANALYSIS_DB_HOST`, `ANALYSIS_DB_PORT`, `ANALYSIS_DB_USER`, `ANALYSIS_DB_PASSWORD`, `ANALYSIS_DB_NAME`, `ANALYSIS_DB_ROOT_PASSWORD` |
| OpenSearch (read-only) | `OPENSEARCH_URL`, `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_USE_SSL`, `OPENSEARCH_VERIFY_CERTS` |
| AI | `OPENAI_API_KEY` (not needed with `--no-ai`), `OPENAI_MODEL` (optional) |

The full list with defaults is in [README.md › Configuration](README.md#4-configuration).

---

## 2. Connection checks

```bash
python -m app.auth                 # every connection, one after the other
python -m app.auth aws             # AWS only
python -m app.auth azure           # Azure only (tenant, identity, subscription, workspace)
python -m app.auth mysql           # company database (read-only session)
python -m app.auth analysis-db     # local analysis database (docker compose)
python -m app.auth opensearch      # OpenSearch (login, roles, cluster)
python -m app.auth aws azure       # several at once
```

Each check prints ✔ or ✘ with hints. Exit code is the worst of the checks run; an unknown
name exits with `2`.

---

## 3. Local analysis database

```bash
docker compose up -d               # start MySQL (analysis data)
docker compose down                # stop, data kept
docker compose down -v             # stop and DELETE all analysis data
python -m app.db status            # migrations applied/pending, row counts per table
python -m app.db migrate           # apply pending migrations now (the first run does it too)
```

---

## 4. AI analysis (recommended entry point)

Runs the evidence pipeline for the cloud, then the AI engine, and writes the recommendations.

```bash
python -m app.reasoning                              # AWS EC2, default window (30d = month to date)
python -m app.reasoning 7d                           # AWS EC2, last 7 days
python -m app.reasoning 7d --region eu-west-1        # AWS EC2, another region
python -m app.reasoning --cloud azure                # Azure VMs, every location
python -m app.reasoning --cloud azure 7d --location eastus   # Azure VMs, one location
python -m app.reasoning 7d --no-ai                   # rules only, no OpenAI call
python -m app.reasoning -e evidence/aws/ec2/<account>/<region>/7d.json      # reuse AWS evidence
python -m app.reasoning -e evidence/azure/vm/<subscription>/all/7d.json     # reuse Azure evidence
python -m app.reasoning 7d -o rec.json -p            # custom output file, also print the JSON
```

| Option | Meaning |
|---|---|
| `WINDOW` | `1h`, `1d`, `3d`, `7d`, `30d` (default), `90d`; see [Analysis windows](#9-analysis-windows) |
| `--cloud aws\|azure` | Cloud to analyse when the pipeline runs (default `aws`; `-e` uses the evidence's cloud) |
| `--region REGION` | AWS region (default `AWS_DEFAULT_REGION`) |
| `--location LOCATION` | Azure location, e.g. `eastus` (default: every location) |
| `-e, --evidence FILE` | Use an existing evidence file instead of running the pipeline |
| `--no-ai` | Rule-based decisions only |
| `-o, --output FILE` | Recommendations file to create or update |
| `-p, --print` | Also print the recommendations JSON |

---

## 5. AWS EC2

Discovery and analysis of the AWS account in `.env`. Every subcommand accepts
`--region REGION` (default `AWS_DEFAULT_REGION`).

### 5.1 Discover

```bash
python -m app.cloud.aws.ec2                              # list instances (default command)
python -m app.cloud.aws.ec2 list --state running         # only running (repeat --state for several)
python -m app.cloud.aws.ec2 config                       # full configuration of every instance
python -m app.cloud.aws.ec2 config -r i-0abc123          # one instance by ID
python -m app.cloud.aws.ec2 config -r api-01             # one instance by Name tag
```

| Subcommand | Alias | Options |
|---|---|---|
| `list` | (default) | `--state STATE` (`pending`, `running`, `shutting-down`, `stopping`, `stopped`, `terminated`; default: all except terminated) |
| `configuration` | `config` | `--state STATE`, `-r, --resource ID_OR_NAME` |

### 5.2 Analyse and metrics

```bash
python -m app.cloud.aws.ec2 run                          # analysis, month to date
python -m app.cloud.aws.ec2 run 7d                       # analysis, last 7 days
python -m app.cloud.aws.ec2 metrics                      # quick CPU/memory, default window
python -m app.cloud.aws.ec2 metrics 7d -r api-01         # one instance
python -m app.cloud.aws.ec2 metrics -d yesterday         # one UTC day, every hour
python -m app.cloud.aws.ec2 metrics -t "2026-11-10 06"   # one UTC hour
python -m app.cloud.aws.ec2 metrics 1d -H                # also list every hourly average
python -m app.cloud.aws.ec2 metrics -o                   # stored data only, no CloudWatch calls
python -m app.cloud.aws.ec2 history                      # stored configuration changes, newest first
python -m app.cloud.aws.ec2 history -r i-0abc123 -n 50   # one instance, 50 changes
```

| Subcommand | Alias | Options |
|---|---|---|
| `run` | `analyze` | `WINDOW` |
| `metrics` | `usage` | `WINDOW`, `-d, --day DAY` (`YYYY-MM-DD`, `today`, `yesterday`) or `-t, --hour HOUR` (`'YYYY-MM-DD HH'` or `HH`), `-r, --resource`, `-H, --hourly`, `-o, --offline` |
| `history` | | `-r, --resource`, `-n, --limit COUNT` (default 20) |

### 5.3 Evidence

```bash
python -m app.cloud.aws.ec2 evidence 7d                  # 9-step pipeline, writes the evidence file
python -m app.cloud.aws.ec2 evidence 7d -p --no-file     # print only, write nothing
python -m app.cloud.aws.ec2 evidence 7d -o ec2-7d.json   # custom evidence file
python -m app.cloud.aws.ec2 validate FILE.json           # check an evidence file against the schema
```

| Subcommand | Alias | Options |
|---|---|---|
| `evidence` | `prepare` | `WINDOW`, `-o, --output FILE`, `--no-file`, `-p, --print` |
| `validate` | | `FILE` |

### 5.4 Prices

```bash
python -m app.cloud.aws.ec2.pricing t3a.medium t3a.small                       # on-demand, Linux
python -m app.cloud.aws.ec2.pricing m5.large --os Windows --region eu-west-1
python -m app.cloud.aws.ec2.pricing m5.large --tenancy Dedicated
```

Options: `--region`, `--os` (`Linux`, `RHEL`, `SUSE`, `Ubuntu Pro`, `Windows`; default
`Linux`), `--tenancy` (`Shared`, `Dedicated`, `Host`; default `Shared`).

---

## 6. Azure VMs

Discovery and analysis of the subscription in `.env`. Every subcommand except `validate`
accepts `--location LOCATION` (default: every location).

### 6.1 Discover

```bash
python -m app.cloud.azure.vm                             # list VMs (default command)
python -m app.cloud.azure.vm list --location eastus      # one location
python -m app.cloud.azure.vm config                      # full configuration of every VM
python -m app.cloud.azure.vm config -r web-01            # one VM by name, vmId or resource ID
```

### 6.2 Analyse and evidence

```bash
python -m app.cloud.azure.vm run                         # analysis, month to date
python -m app.cloud.azure.vm run 7d                      # analysis, last 7 days
python -m app.cloud.azure.vm evidence 7d                 # 9-step pipeline, writes the evidence file
python -m app.cloud.azure.vm evidence 7d -p --no-file    # print only, write nothing
python -m app.cloud.azure.vm validate FILE.json          # check an evidence file against the schema
```

| Subcommand | Alias | Options |
|---|---|---|
| `list` | (default) | `--location` |
| `configuration` | `config` | `--location`, `-r, --resource NAME_OR_ID` |
| `run` | `analyze` | `WINDOW`, `--location` |
| `evidence` | `prepare` | `WINDOW`, `--location`, `-o, --output FILE`, `--no-file`, `-p, --print` |
| `validate` | | `FILE` |

Azure has no `metrics` or `history` subcommand; use `run` for the metrics report.

### 6.3 Prices

```bash
python -m app.cloud.azure.vm.pricing Standard_D2s_v5 Standard_D4s_v5 --location eastus
python -m app.cloud.azure.vm.pricing Standard_E4s_v5 --location westeurope --os Windows
```

Options: `--location` (required), `--os` (`Linux`, `Windows`; default `Linux`).

---

## 7. Company database queries

Read-only queries against the company database (`DB_*`, READ ONLY session). They build on
each other: tenants → module → audits → newest first.

```bash
python -m app.server.mysql.active_tenants                    # active, non-deleted tenants
python -m app.server.mysql.active_tenants --json             # tenant IDs as a JSON array
python -m app.server.mysql.ingestion_modules                 # the super_index module row
python -m app.server.mysql.ingestion_modules other_module    # another module by name
python -m app.server.mysql.ingestion_modules --json          # the row as JSON
python -m app.server.mysql.data_ingestion_audit              # completed super_index audits of 2026, per tenant
python -m app.server.mysql.data_ingestion_audit --year 2025  # another year
python -m app.server.mysql.data_ingestion_audit --json       # the rows as JSON
python -m app.server.mysql.completed_audits_latest           # the same audits, newest first
python -m app.server.mysql.completed_audits_latest -n 20     # only the 20 most recent
python -m app.server.mysql.completed_audits_latest --json    # the ordered rows as JSON
```

| Command | What it reads | Options |
|---|---|---|
| `active_tenants` | `tenants` where `tenant_status = 'active'` and `is_deleted = 0` | `-j, --json` |
| `ingestion_modules` | `ingestion_modules` where `module_name` matches (default `super_index`) | `MODULE_NAME`, `-j, --json` |
| `data_ingestion_audit` | `data_ingestion_audit` of the active tenants, one module, year and status | `--year` (2026), `--status` (`COMPLETED`), `--module` (`super_index`), `-j, --json` |
| `completed_audits_latest` | same rows, most recently completed first | same, plus `-n, --limit COUNT` |

---

## 8. OpenSearch super index

Reads `super_index_<tenant_id>` of every active tenant (read-only), keeps documents with a
`timestamp` in the year, matches each document's `job_id` to a completed audit and lists
the jobs newest first. A live progress line per step shows on stderr while fetching.

```bash
python -m app.server.opensearch.aws.ec2.ec2_super_index      # AWS EC2 documents
python -m app.server.opensearch.azure.vm_super_index         # Azure VM documents
```

| Command | Filter | Saved file |
|---|---|---|
| `aws.ec2.ec2_super_index` | `cloud_type.keyword = AWS`, `product_code.keyword = AmazonEC2` | `opensearch_results/aws_ec2_opensearch.json` |
| `azure.vm_super_index` | `cloud_type.keyword = AZURE`, `service_type.keyword = microsoft.compute_virtualmachines` | `opensearch_results/azure_opensearch.json` |

Both commands take the same options:

```bash
python -m app.server.opensearch.azure.vm_super_index --show 0       # list every document per job
python -m app.server.opensearch.azure.vm_super_index --year 2025    # timestamps and audits of 2025
python -m app.server.opensearch.azure.vm_super_index --json         # the ordered object on stdout
python -m app.server.opensearch.azure.vm_super_index -q             # no progress lines
python -m app.server.opensearch.azure.vm_super_index -o vms.json    # save the hits to another file
```

| Option | Meaning |
|---|---|
| `--year YEAR` | Year of `audit_date` and document `timestamp` (default 2026) |
| `--status STATUS` | Audit status (default `COMPLETED`) |
| `--module MODULE` | Ingestion module (default `super_index`) |
| `--show COUNT` | Documents listed per job in the terminal, `0` for all (default 5) |
| `-j, --json` | Print the ordered object (jobs with their documents) as JSON |
| `-q, --quiet` | No progress lines |
| `-o, --output PATH` | Where to save the hits (default: the saved file in the table above) |

The saved file is a JSON array of the matched hits exactly as OpenSearch returned them
(`_index`, `_id`, `_score`, `_source`), newest completed job first. Every run replaces the
same file; no numbered copies are made.

---

## 9. Analysis windows

| Key | Purpose | Covers |
|---|---|---|
| `1h` | testing | the last settled hour |
| `1d` | testing | rolling 24 hours |
| `3d` | testing | rolling 3 days |
| `7d` | testing | rolling 7 days |
| `30d` | production (default) | month to date, from the 1st of this month 00:00 UTC |
| `90d` | production | last 3 calendar months to date |

Every window ends at the last settled hour. Windows are defined in `app/config.py`.

---

## 10. Output files

All output folders are git-ignored. Each file is replaced in place on the next run.

| Path | Written by |
|---|---|
| `evidence/aws/ec2/<account>/<region>/<window>.json` | `app.cloud.aws.ec2 evidence`, `app.reasoning` |
| `evidence/azure/vm/<subscription>/<location\|all>/<window>.json` | `app.cloud.azure.vm evidence`, `app.reasoning --cloud azure` |
| `recommendations/aws/ec2/<account>/<region>/<window>.json` | `app.reasoning` |
| `recommendations/azure/vm/<subscription>/<location\|all>/<window>.json` | `app.reasoning --cloud azure` |
| `opensearch_results/aws_ec2_opensearch.json` | `app.server.opensearch.aws.ec2.ec2_super_index` |
| `opensearch_results/azure_opensearch.json` | `app.server.opensearch.azure.vm_super_index` |

---

## 11. Everyday recipes

**Production run, both clouds (month to date):**

```bash
python -m app.reasoning
python -m app.reasoning --cloud azure
```

**Quick test without AI cost:**

```bash
python -m app.reasoning 1d --no-ai
```

**Prepare evidence now, reason later:**

```bash
python -m app.cloud.azure.vm evidence 7d
python -m app.reasoning -e evidence/azure/vm/<subscription>/all/7d.json
```

**Inspect one machine:**

```bash
python -m app.cloud.aws.ec2 config -r api-01
python -m app.cloud.azure.vm config -r web-01
python -m app.cloud.aws.ec2 history -r i-0abc123
```

**Latest ingestion data from OpenSearch:**

```bash
python -m app.server.mysql.completed_audits_latest -n 10     # which jobs completed last
python -m app.server.opensearch.aws.ec2.ec2_super_index      # EC2 documents of those jobs
python -m app.server.opensearch.azure.vm_super_index         # Azure VM documents of those jobs
```

**Health check:**

```bash
python -m app.auth
python -m app.db status
```

---

## 12. Exit codes and quick fixes

| Exit code | Meaning |
|---|---|
| `0` | Success (also when nothing matched) |
| `1` | A step failed: connection, permission, validation or file write; the message and hints say why |
| `2` | Invalid arguments, or an unknown name for `python -m app.auth` |

| Symptom | Fix |
|---|---|
| `✘ … is not set in .env` | Add the variable named in the message to `.env` |
| Connection or permission failure | Run `python -m app.auth <name>` and follow the hints |
| Analysis database refused | `docker compose up -d`, then `python -m app.auth analysis-db` |
| `OPENAI_API_KEY is not set` | Add it to `.env`, or run with `--no-ai` |
| OpenSearch line stuck on the same count while the clock runs | That request is waiting on OpenSearch; check `python -m app.auth opensearch` |
| `no index super_index_<id>` | That tenant has no super index yet; not an error |
| `fatal: not a git repository` | Run git commands from the project root |

More detail: [README.md › Troubleshooting](README.md#13-troubleshooting-and-exit-codes),
[app/cloud/aws/ec2/README.md](app/cloud/aws/ec2/README.md),
[app/cloud/azure/vm/README.md](app/cloud/azure/vm/README.md).
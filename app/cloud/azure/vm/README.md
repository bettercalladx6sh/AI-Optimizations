# Azure VM discovery, analysis and evidence

The Azure counterpart of `app/cloud/aws/ec2`: read-only discovery, change tracking, hourly
CPU/memory utilization, cumulative averages, validated rightsizing evidence and pay-as-you-go
pricing for the VMs of the subscription configured in `.env`.

- **Azure is only read.** Every call is a GET (or a read-only Log Analytics query).
- **The company database is only read.** Everything this module writes goes to the
  **local analysis database** (docker compose).
- **Azure Monitor is called as little as possible.** Every hour of every metric is fetched at
  most once; a run where nothing is missing makes no metric calls.
- **Evidence is not stored in the database.** It is built in memory for each run, validated,
  and written to one JSON file that the next run of the same window replaces.

## Commands

| Command | What it does |
|---|---|
| `python -m app.cloud.azure.vm` | List VMs: status, location, resource group |
| `python -m app.cloud.azure.vm config [-r NAME_OR_ID]` | Full configuration: size, vCPU, memory, OS, image, licence, placement, disks, tags |
| `python -m app.cloud.azure.vm run [WINDOW]` | Analysis: configuration, changes, missing metric hours, cumulative averages, readiness |
| `python -m app.cloud.azure.vm evidence [WINDOW]` | The 9-step evidence pipeline (analysis, then validated JSON) |
| `python -m app.cloud.azure.vm validate FILE` | Check an evidence file against the schema |
| `python -m app.cloud.azure.vm.pricing SIZE ... --location L [--os Windows]` | Pay-as-you-go list prices |
| `python -m app.reasoning --cloud azure [WINDOW]` | Evidence pipeline, then AI rightsizing recommendations |

Every command takes `--location LOCATION` (e.g. `eastus`); without it every location of the
subscription is covered (scope `all`). Windows are the same as for EC2: `30d` (month to date,
default), `90d` (last 3 calendar months to date), and the testing windows `1h`, `1d`, `3d`, `7d`.

Output files:

    evidence/azure/vm/<subscription>/<location|all>/<window>.json
    recommendations/azure/vm/<subscription>/<location|all>/<window>.json

## Authentication (.env)

    AZURE_SUBSCRIPTION_ID=            subscription to analyse (required)
    AZURE_TENANT_ID=                  tenant of the managed identity / service principal
    AZURE_TARGET_TENANT_ID=           tenant that holds the subscription (cross-tenant; defaults to AZURE_TENANT_ID)
    AZURE_APP_CLIENT_ID=              multi-tenant app registration trusted by the managed identity
    AZURE_UAMI_CLIENT_ID=             user-assigned managed identity
    AZURE_LOG_ANALYTICS_WORKSPACE_ID= workspace with VM insights memory (optional, recommended)

With `AZURE_UAMI_CLIENT_ID` and `AZURE_APP_CLIENT_ID` set, the managed identity gets a token for
`api://AzureADTokenExchange` and uses it as the client assertion of the app registration in the
target tenant (federated credential, no secret). Otherwise a service principal secret, the
Azure CLI login or the VM's managed identity is used. Check it with `python -m app.auth azure`.

Roles needed (all read-only), granted to the identity the target tenant sees:

    Reader                 on the subscription   VMs, disks, resource SKUs
    Monitoring Reader      on the subscription   Azure Monitor platform metrics
    Log Analytics Reader   on the workspace      memory from VM insights (only with the workspace set)

## How metrics are collected

    CPU      Azure Monitor platform metric "Percentage CPU", hourly average
    Memory   with AZURE_LOG_ANALYTICS_WORKSPACE_ID: InsightsMetrics Memory/AvailableMB from
             VM insights (Azure Monitor Agent), as used % = 100 x (1 - available / memorySizeMB)
             without it: platform metric "Available Memory Bytes", as used % of the VM size's memory

Platform metrics are read per VM, at most 30 days per call; Log Analytics memory is read for up
to 100 VMs per query. Throttling (429) and server errors are retried with backoff and
Retry-After. Without a workspace, memory is available only where Azure publishes Available
Memory Bytes; VMs without it get `METRIC_MISSING` and the AI engine returns INSUFFICIENT_DATA
for them instead of guessing.

## Identity of a VM

`instance_id` in the database and in evidence is the VM's immutable **vmId** (lower case). A VM
that is deleted and recreated with the same name is a new VM. A resize, an OS, licence, priority
or disk change restarts the evaluation window; a rename, a stop/start or a boot-diagnostics
change does not.

## Pricing

Prices come from the public Azure Retail Prices API: type `Consumption`, hourly meters only,
no Spot or Low Priority meters, no reservations or savings plans, and a size is priced only
when Azure returns exactly one price for it. Windows VMs use the Windows meter; with Azure
Hybrid Benefit (`licenseType` Windows_Server) and RHEL/SLES BYOS the Linux rate applies. Spot
VMs, dedicated hosts, marketplace plans and pay-as-you-go RHEL, SUSE, SQL Server or Ubuntu Pro
images get a reason and no price, because their bill is not the VM size price alone.

Rightsizing candidates are the sizes of the VM's own family (e.g. `standardDSv5Family`) with the
same CPU architecture that the subscription can deploy in the VM's location; constrained-vCPU
sizes (e.g. `Standard_E8-2s_v5`) are excluded because they are billed as the full size.

## Files in this folder

| File | Purpose |
|---|---|
| `__main__.py` | Command line |
| `inventory.py` | VM listing, power states, managed disks, configuration output |
| `skus.py` | VM size specs and family members per location (Compute Resource SKUs) |
| `changes.py` | Tracked fields, snapshots and change detection |
| `metrics.py` | Metric definitions and fetching (Azure Monitor, Log Analytics) |
| `store.py` | Writes to the local database (`azure_vms`, `azure_vm_disks`, `azure_vm_metric_*`) |
| `analysis.py` | The six analysis steps and their output |
| `evidence.py` | Evidence document and validation |
| `pipeline.py` | The nine-step evidence pipeline |
| `pricing.py` | Azure Retail Prices API |
| `schemas/azure_vm_evidence.schema.json` | JSON Schema of the evidence |

Shared with EC2: `app/cloud/{timeseries,windows,cumulative,utilization,evidence,evidence_output,pricing,backoff,display}.py`
and `app/db/metrics.py`. REST calls go through `app/cloud/azure/rest.py`. Tables are created by
migration `app/db/migrations/0006_azure_vm_tables.sql`, applied automatically by the next run.

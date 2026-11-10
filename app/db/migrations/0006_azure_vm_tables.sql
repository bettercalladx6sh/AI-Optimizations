-- 0006: Azure VM tables (the generic resources, snapshots, changes and tags tables are shared).
--
--   azure_vms                    current specs of each Azure VM (resources.resource_id = vmId)
--   azure_vm_disks               managed disks currently attached to each VM
--   azure_vm_metric_datapoints   one row per VM, metric and hour: the hourly average
--   azure_vm_metric_fetches      every hour range requested from Azure Monitor / Log Analytics;
--                                a checked range is never requested again
--   azure_vm_metric_collections  what each analysis run found per VM and metric
--   azure_vm_metric_cumulative   statistics per VM, metric and configuration version
--
-- The metric tables have the same columns as the EC2 ones (instance_id = the VM's vmId) and
-- are written by the shared app/db/metrics.py.
--
-- Metrics: cpu_utilization    (Microsoft.Compute/virtualMachines Percentage CPU, Percent)
--          memory_utilization (Log Analytics InsightsMetrics Memory/AvailableMB, or
--                              Available Memory Bytes; stored as used Percent)

ALTER TABLE analysis_runs
    MODIFY COLUMN window_key VARCHAR(16) NOT NULL COMMENT 'window key from app/config.py';

CREATE TABLE azure_vms (
    resource_ref     BIGINT UNSIGNED PRIMARY KEY,
    arm_id           VARCHAR(512)   NOT NULL COMMENT 'Azure Resource Manager ID',
    resource_group   VARCHAR(90)    NOT NULL,
    vm_size          VARCHAR(64)    NOT NULL,
    family           VARCHAR(64)    NULL,
    vcpus            SMALLINT UNSIGNED NULL,
    memory_gib       DECIMAL(10, 3) NULL,
    architecture     VARCHAR(16)    NULL,
    burstable        BOOLEAN        NULL,
    os_type          VARCHAR(16)    NULL,
    image            VARCHAR(255)   NULL,
    marketplace_plan VARCHAR(255)   NULL,
    license_type     VARCHAR(32)    NULL,
    priority         VARCHAR(16)    NULL,
    eviction_policy  VARCHAR(16)    NULL,
    location         VARCHAR(64)    NOT NULL,
    zone             VARCHAR(16)    NULL,
    availability_set VARCHAR(80)    NULL,
    scale_set        VARCHAR(80)    NULL,
    dedicated_host   BOOLEAN        NOT NULL,
    boot_diagnostics BOOLEAN        NOT NULL,
    storage_gib      INT UNSIGNED   NOT NULL,
    time_created     DATETIME(6)    NULL,
    updated_at       DATETIME(6)    NOT NULL,
    INDEX idx_azure_vms_size (vm_size),
    CONSTRAINT fk_azure_vms_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE azure_vm_disks (
    resource_ref    BIGINT UNSIGNED NOT NULL,
    disk_name       VARCHAR(80)  NOT NULL,
    role            VARCHAR(8)   NOT NULL COMMENT 'os, data',
    lun             SMALLINT UNSIGNED NULL,
    size_gib        INT UNSIGNED NULL,
    sku             VARCHAR(32)  NULL,
    iops            INT UNSIGNED NULL,
    throughput_mbps INT UNSIGNED NULL,
    encryption      VARCHAR(64)  NULL,
    delete_option   VARCHAR(16)  NULL,
    PRIMARY KEY (resource_ref, disk_name),
    CONSTRAINT fk_azure_vm_disks_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE azure_vm_metric_datapoints (
    resource_ref   BIGINT UNSIGNED NOT NULL,
    instance_id    VARCHAR(64)  NOT NULL COMMENT 'vmId',
    instance_name  VARCHAR(255) NULL,
    metric         VARCHAR(32)  NOT NULL COMMENT 'cpu_utilization, memory_utilization',
    period_start   DATETIME(6)  NOT NULL COMMENT 'start of the hour (UTC)',
    period_seconds INT UNSIGNED NOT NULL COMMENT '3600',
    average        DOUBLE       NOT NULL COMMENT 'hourly average',
    unit           VARCHAR(16)  NOT NULL COMMENT 'Percent',
    run_id         BIGINT UNSIGNED NOT NULL COMMENT 'run that last collected this datapoint',
    collected_at   DATETIME(6)  NOT NULL,
    PRIMARY KEY (resource_ref, metric, period_start),
    INDEX idx_azure_vm_metric_datapoints_run (run_id),
    INDEX idx_azure_vm_metric_datapoints_instance (instance_id, metric, period_start),
    CONSTRAINT fk_azure_vm_metric_datapoints_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE,
    CONSTRAINT fk_azure_vm_metric_datapoints_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE azure_vm_metric_fetches (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id          BIGINT UNSIGNED NOT NULL,
    resource_ref    BIGINT UNSIGNED NOT NULL,
    instance_id     VARCHAR(64)  NOT NULL,
    instance_name   VARCHAR(255) NULL,
    metric          VARCHAR(32)  NOT NULL,
    range_start     DATETIME(6)  NOT NULL COMMENT 'first hour requested (UTC)',
    range_end       DATETIME(6)  NOT NULL COMMENT 'end of the last hour requested (exclusive)',
    requested_hours INT UNSIGNED NOT NULL,
    returned_points INT UNSIGNED NOT NULL,
    fetched_at      DATETIME(6)  NOT NULL,
    INDEX idx_azure_vm_metric_fetches_series (resource_ref, metric, range_start),
    INDEX idx_azure_vm_metric_fetches_instance (instance_id, metric),
    CONSTRAINT fk_azure_vm_metric_fetches_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id),
    CONSTRAINT fk_azure_vm_metric_fetches_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE azure_vm_metric_collections (
    run_id           BIGINT UNSIGNED NOT NULL,
    resource_ref     BIGINT UNSIGNED NOT NULL,
    instance_id      VARCHAR(64)  NOT NULL,
    instance_name    VARCHAR(255) NULL,
    metric           VARCHAR(32)  NOT NULL,
    namespace        VARCHAR(255) NOT NULL,
    metric_name      VARCHAR(255) NOT NULL,
    statistic        VARCHAR(16)  NOT NULL COMMENT 'Average',
    period_seconds   INT UNSIGNED NOT NULL,
    window_start     DATETIME(6)  NOT NULL,
    window_end       DATETIME(6)  NOT NULL,
    expected_points  INT UNSIGNED NOT NULL,
    requested_points INT UNSIGNED NOT NULL DEFAULT 0,
    fetched_points   INT UNSIGNED NOT NULL DEFAULT 0,
    collected_points INT UNSIGNED NOT NULL,
    PRIMARY KEY (run_id, resource_ref, metric),
    INDEX idx_azure_vm_metric_collections_instance (instance_id, metric),
    CONSTRAINT fk_azure_vm_metric_collections_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id),
    CONSTRAINT fk_azure_vm_metric_collections_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE azure_vm_metric_cumulative (
    resource_ref          BIGINT UNSIGNED NOT NULL,
    instance_id           VARCHAR(64)  NOT NULL,
    instance_name         VARCHAR(255) NULL,
    metric                VARCHAR(32)  NOT NULL,
    configuration_version CHAR(64)     NOT NULL,
    effective_since       DATETIME(6)  NULL COMMENT 'when this configuration was detected (NULL: since tracking began)',
    evidence_start        DATETIME(6)  NULL COMMENT 'first hour included (UTC)',
    evidence_end          DATETIME(6)  NULL COMMENT 'last hour included (UTC)',
    datapoint_count       INT UNSIGNED NOT NULL,
    value_sum             DOUBLE       NOT NULL,
    average               DOUBLE       NULL COMMENT 'cumulative average = value_sum / datapoint_count',
    minimum               DOUBLE       NULL,
    maximum               DOUBLE       NULL,
    p50                   DOUBLE       NULL,
    p95                   DOUBLE       NULL,
    p99                   DOUBLE       NULL,
    standard_deviation    DOUBLE       NULL,
    trend                 VARCHAR(16)  NULL COMMENT 'GROWING, STABLE, DECLINING',
    volatility            VARCHAR(16)  NULL COMMENT 'LOW, MODERATE, HIGH, STABLE',
    coverage_percent      DOUBLE       NOT NULL,
    gap_count             INT UNSIGNED NOT NULL,
    run_id                BIGINT UNSIGNED NOT NULL COMMENT 'run that last rebuilt this summary',
    updated_at            DATETIME(6)  NOT NULL,
    PRIMARY KEY (resource_ref, metric, configuration_version),
    INDEX idx_azure_vm_metric_cumulative_instance (instance_id, metric),
    CONSTRAINT fk_azure_vm_metric_cumulative_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE,
    CONSTRAINT fk_azure_vm_metric_cumulative_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

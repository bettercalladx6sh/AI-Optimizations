-- 0005: cumulative EC2 metric summaries per configuration version.
--
--   ec2_metric_cumulative  one row per instance, metric and configuration version: statistics
--                          over every stored hourly average since that configuration became
--                          effective (cumulative average, min, max, p50/p95/p99, standard
--                          deviation, trend, volatility, coverage, gaps).
--
-- configuration_version is a SHA-256 of the instance's specs (type, size, vCPUs, memory,
-- storage, ...), so a resize starts a new version while a rename or stop/start does not.
-- Rows are rebuilt from ec2_metric_datapoints on every analysis run; the raw datapoints
-- stay authoritative.

CREATE TABLE ec2_metric_cumulative (
    resource_ref          BIGINT UNSIGNED NOT NULL,
    instance_id           VARCHAR(32)  NOT NULL,
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
    INDEX idx_ec2_metric_cumulative_instance (instance_id, metric),
    CONSTRAINT fk_ec2_metric_cumulative_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE,
    CONSTRAINT fk_ec2_metric_cumulative_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
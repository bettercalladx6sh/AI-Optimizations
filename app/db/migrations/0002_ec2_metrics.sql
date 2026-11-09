-- 0002: EC2 utilization metrics from CloudWatch (AWS only; Azure gets its own tables).
--
--   ec2_metric_datapoints   one row per instance, metric and hour: the hourly average
--                           (24 rows per instance, metric and day)
--   ec2_metric_collections  what each analysis run collected per instance and metric:
--                           source metric, time range, expected vs collected datapoints
--
-- Metrics: cpu_utilization    (AWS/EC2    CPUUtilization,   Percent)
--          memory_utilization (EC2/Custom mem_used_percent, Percent)

CREATE TABLE ec2_metric_datapoints (
    resource_ref   BIGINT UNSIGNED NOT NULL,
    metric         VARCHAR(32)  NOT NULL COMMENT 'cpu_utilization, memory_utilization',
    period_start   DATETIME(6)  NOT NULL COMMENT 'start of the hour (UTC)',
    period_seconds INT UNSIGNED NOT NULL COMMENT '3600',
    average        DOUBLE       NOT NULL COMMENT 'hourly average',
    unit           VARCHAR(16)  NOT NULL COMMENT 'Percent',
    run_id         BIGINT UNSIGNED NOT NULL COMMENT 'run that last collected this datapoint',
    collected_at   DATETIME(6)  NOT NULL,
    PRIMARY KEY (resource_ref, metric, period_start),
    INDEX idx_ec2_metric_datapoints_run (run_id),
    CONSTRAINT fk_ec2_metric_datapoints_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE,
    CONSTRAINT fk_ec2_metric_datapoints_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE ec2_metric_collections (
    run_id           BIGINT UNSIGNED NOT NULL,
    resource_ref     BIGINT UNSIGNED NOT NULL,
    metric           VARCHAR(32)  NOT NULL,
    namespace        VARCHAR(255) NOT NULL,
    metric_name      VARCHAR(255) NOT NULL,
    statistic        VARCHAR(16)  NOT NULL COMMENT 'Average',
    period_seconds   INT UNSIGNED NOT NULL,
    window_start     DATETIME(6)  NOT NULL,
    window_end       DATETIME(6)  NOT NULL,
    expected_points  INT UNSIGNED NOT NULL,
    collected_points INT UNSIGNED NOT NULL,
    PRIMARY KEY (run_id, resource_ref, metric),
    CONSTRAINT fk_ec2_metric_collections_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id),
    CONSTRAINT fk_ec2_metric_collections_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
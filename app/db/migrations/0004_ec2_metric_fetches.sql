-- 0004: fetch only missing EC2 metric hours.
--
--   ec2_metric_fetches      every hour range requested from CloudWatch, per instance and
--                           metric. A checked range is never requested again, even when
--                           CloudWatch had no data for it (e.g. no memory agent, instance
--                           stopped), so each hour costs at most one request.
--   ec2_metric_collections  gains requested_points (hours requested from CloudWatch in that
--                           run) and fetched_points (new datapoints stored by that run).
--                           collected_points now means datapoints available in the window.
--
-- Existing runs fetched their whole window, so their windows are recorded as checked.
-- The last hour of each is left out because those runs did not wait for it to settle.

CREATE TABLE ec2_metric_fetches (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id          BIGINT UNSIGNED NOT NULL,
    resource_ref    BIGINT UNSIGNED NOT NULL,
    instance_id     VARCHAR(32)  NOT NULL,
    instance_name   VARCHAR(255) NULL,
    metric          VARCHAR(32)  NOT NULL,
    range_start     DATETIME(6)  NOT NULL COMMENT 'first hour requested (UTC)',
    range_end       DATETIME(6)  NOT NULL COMMENT 'end of the last hour requested (exclusive)',
    requested_hours INT UNSIGNED NOT NULL,
    returned_points INT UNSIGNED NOT NULL,
    fetched_at      DATETIME(6)  NOT NULL,
    INDEX idx_ec2_metric_fetches_series (resource_ref, metric, range_start),
    INDEX idx_ec2_metric_fetches_instance (instance_id, metric),
    CONSTRAINT fk_ec2_metric_fetches_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id),
    CONSTRAINT fk_ec2_metric_fetches_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO ec2_metric_fetches
    (run_id, resource_ref, instance_id, instance_name, metric, range_start, range_end,
     requested_hours, returned_points, fetched_at)
SELECT c.run_id, c.resource_ref, c.instance_id, c.instance_name, c.metric,
       c.window_start, DATE_SUB(c.window_end, INTERVAL 1 HOUR),
       c.expected_points - 1, c.collected_points, a.started_at
FROM ec2_metric_collections c
JOIN analysis_runs a ON a.id = c.run_id
WHERE c.window_end > DATE_ADD(c.window_start, INTERVAL 1 HOUR);

ALTER TABLE ec2_metric_collections
    ADD COLUMN requested_points INT UNSIGNED NOT NULL DEFAULT 0 AFTER expected_points,
    ADD COLUMN fetched_points   INT UNSIGNED NOT NULL DEFAULT 0 AFTER requested_points;

UPDATE ec2_metric_collections
    SET requested_points = expected_points, fetched_points = collected_points;
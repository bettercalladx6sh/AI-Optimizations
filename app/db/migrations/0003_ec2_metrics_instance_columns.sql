-- 0003: add instance_id and instance_name to the EC2 metrics tables.
--
-- Both tables already link to the instance through resource_ref; these columns make
-- the rows readable on their own (e.g. WHERE instance_id = 'i-0abc123').
--   instance_id    AWS instance ID, e.g. i-0abc123 (never changes)
--   instance_name  value of the Name tag when the row was collected (may be empty)
-- Existing rows are filled in from the resources table.

ALTER TABLE ec2_metric_datapoints
    ADD COLUMN instance_id   VARCHAR(32)  NULL AFTER resource_ref,
    ADD COLUMN instance_name VARCHAR(255) NULL AFTER instance_id;

UPDATE ec2_metric_datapoints d
    JOIN resources r ON r.id = d.resource_ref
    SET d.instance_id = r.resource_id, d.instance_name = r.name;

ALTER TABLE ec2_metric_datapoints
    MODIFY COLUMN instance_id VARCHAR(32) NOT NULL,
    ADD INDEX idx_ec2_metric_datapoints_instance (instance_id, metric, period_start);

ALTER TABLE ec2_metric_collections
    ADD COLUMN instance_id   VARCHAR(32)  NULL AFTER resource_ref,
    ADD COLUMN instance_name VARCHAR(255) NULL AFTER instance_id;

UPDATE ec2_metric_collections c
    JOIN resources r ON r.id = c.resource_ref
    SET c.instance_id = r.resource_id, c.instance_name = r.name;

ALTER TABLE ec2_metric_collections
    MODIFY COLUMN instance_id VARCHAR(32) NOT NULL,
    ADD INDEX idx_ec2_metric_collections_instance (instance_id, metric);
-- 0001: initial schema for the analysis database.
--
-- Generic tables (any provider, any service):
--   cloud_accounts      AWS accounts / Azure subscriptions that have been analysed
--   analysis_runs       one row per analysis run
--   resources           every discovered resource and its current state
--   resource_snapshots  every distinct configuration a resource has had
--   resource_changes    every detected change, linked to the snapshots before and after
--   resource_tags       current tags of each resource
-- EC2 tables (typed columns for querying the current configuration):
--   ec2_instances       current specs of each EC2 instance
--   ec2_volumes         EBS volumes currently attached to each instance
--
-- Applied once by app/db/database.py. Never edit an applied migration;
-- add a new numbered file instead.

CREATE TABLE cloud_accounts (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    provider      VARCHAR(16)  NOT NULL COMMENT 'aws, azure',
    account_id    VARCHAR(64)  NOT NULL COMMENT 'AWS account ID or Azure subscription ID',
    display_name  VARCHAR(255) NULL,
    first_seen_at DATETIME(6)  NOT NULL,
    last_seen_at  DATETIME(6)  NOT NULL,
    UNIQUE KEY uq_cloud_accounts (provider, account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE analysis_runs (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    account_ref      BIGINT UNSIGNED NOT NULL,
    region           VARCHAR(64)  NOT NULL,
    service          VARCHAR(32)  NOT NULL COMMENT 'ec2, ...',
    window_key       VARCHAR(8)   NOT NULL COMMENT '1h, 1d, 3d, 7d, 30d, 90d',
    window_seconds   INT UNSIGNED NOT NULL,
    purpose          VARCHAR(16)  NOT NULL COMMENT 'production, testing',
    status           ENUM('running', 'succeeded', 'failed') NOT NULL DEFAULT 'running',
    started_at       DATETIME(6)  NOT NULL,
    finished_at      DATETIME(6)  NULL,
    resource_count   INT UNSIGNED NULL,
    change_count     INT UNSIGNED NULL,
    baseline_created BOOLEAN      NOT NULL DEFAULT FALSE,
    error_message    TEXT         NULL,
    INDEX idx_analysis_runs_scope (account_ref, region, service, started_at),
    CONSTRAINT fk_analysis_runs_account FOREIGN KEY (account_ref) REFERENCES cloud_accounts (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE resources (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    account_ref         BIGINT UNSIGNED NOT NULL,
    region              VARCHAR(64)  NOT NULL,
    service             VARCHAR(32)  NOT NULL COMMENT 'ec2, ...',
    resource_type       VARCHAR(32)  NOT NULL COMMENT 'instance, ...',
    resource_id         VARCHAR(255) NOT NULL COMMENT 'provider ID, e.g. i-0abc123',
    name                VARCHAR(255) NULL,
    state               VARCHAR(32)  NULL,
    current_snapshot_id BIGINT UNSIGNED NULL,
    first_seen_at       DATETIME(6)  NOT NULL,
    first_seen_run_id   BIGINT UNSIGNED NOT NULL,
    last_seen_at        DATETIME(6)  NOT NULL,
    last_seen_run_id    BIGINT UNSIGNED NOT NULL,
    last_changed_at     DATETIME(6)  NULL COMMENT 'last change that restarts the evaluation window',
    removed_at          DATETIME(6)  NULL,
    UNIQUE KEY uq_resources (account_ref, region, service, resource_type, resource_id),
    INDEX idx_resources_name (account_ref, region, service, name),
    CONSTRAINT fk_resources_account FOREIGN KEY (account_ref) REFERENCES cloud_accounts (id),
    CONSTRAINT fk_resources_first_run FOREIGN KEY (first_seen_run_id) REFERENCES analysis_runs (id),
    CONSTRAINT fk_resources_last_run FOREIGN KEY (last_seen_run_id) REFERENCES analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE resource_snapshots (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    resource_ref  BIGINT UNSIGNED NOT NULL,
    run_id        BIGINT UNSIGNED NOT NULL COMMENT 'run that first captured this configuration',
    captured_at   DATETIME(6) NOT NULL,
    config_hash   CHAR(64)    NOT NULL COMMENT 'SHA-256 of the configuration document',
    tracked       JSON        NOT NULL COMMENT 'fields compared for change detection',
    configuration JSON        NOT NULL COMMENT 'full configuration as discovered',
    INDEX idx_resource_snapshots_resource (resource_ref, captured_at),
    CONSTRAINT fk_resource_snapshots_resource FOREIGN KEY (resource_ref) REFERENCES resources (id),
    CONSTRAINT fk_resource_snapshots_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE resources
    ADD CONSTRAINT fk_resources_current_snapshot
    FOREIGN KEY (current_snapshot_id) REFERENCES resource_snapshots (id);

CREATE TABLE resource_changes (
    id                 BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    resource_ref       BIGINT UNSIGNED NOT NULL,
    run_id             BIGINT UNSIGNED NOT NULL,
    change_type        ENUM('added', 'removed', 'modified') NOT NULL,
    significant        BOOLEAN     NOT NULL COMMENT 'true when the change restarts the evaluation window',
    detected_at        DATETIME(6) NOT NULL,
    field_changes      JSON        NOT NULL COMMENT '[{"field", "before", "after"}]',
    before_snapshot_id BIGINT UNSIGNED NULL,
    after_snapshot_id  BIGINT UNSIGNED NULL,
    INDEX idx_resource_changes_resource (resource_ref, detected_at),
    INDEX idx_resource_changes_run (run_id),
    CONSTRAINT fk_resource_changes_resource FOREIGN KEY (resource_ref) REFERENCES resources (id),
    CONSTRAINT fk_resource_changes_run FOREIGN KEY (run_id) REFERENCES analysis_runs (id),
    CONSTRAINT fk_resource_changes_before FOREIGN KEY (before_snapshot_id) REFERENCES resource_snapshots (id),
    CONSTRAINT fk_resource_changes_after FOREIGN KEY (after_snapshot_id) REFERENCES resource_snapshots (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE resource_tags (
    resource_ref BIGINT UNSIGNED NOT NULL,
    tag_key      VARCHAR(128) NOT NULL,
    tag_value    VARCHAR(256) NOT NULL,
    PRIMARY KEY (resource_ref, tag_key),
    CONSTRAINT fk_resource_tags_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE ec2_instances (
    resource_ref        BIGINT UNSIGNED PRIMARY KEY,
    instance_type       VARCHAR(32)    NOT NULL,
    family              VARCHAR(16)    NOT NULL,
    size                VARCHAR(16)    NOT NULL,
    vcpus               SMALLINT UNSIGNED NULL,
    memory_gib          DECIMAL(10, 3) NULL,
    network_performance VARCHAR(64)    NULL,
    burstable           BOOLEAN        NULL,
    current_generation  BOOLEAN        NULL,
    architecture        VARCHAR(16)    NULL,
    platform            VARCHAR(64)    NULL,
    lifecycle           VARCHAR(16)    NULL,
    tenancy             VARCHAR(16)    NULL,
    availability_zone   VARCHAR(32)    NULL,
    vpc_id              VARCHAR(32)    NULL,
    subnet_id           VARCHAR(32)    NULL,
    public_ip           VARCHAR(45)    NULL,
    private_ip          VARCHAR(45)    NULL,
    ami_id              VARCHAR(32)    NULL,
    key_name            VARCHAR(255)   NULL,
    iam_profile         VARCHAR(255)   NULL,
    security_groups     JSON           NOT NULL,
    ebs_optimized       BOOLEAN        NOT NULL,
    detailed_monitoring BOOLEAN        NOT NULL,
    root_device         VARCHAR(64)    NULL,
    storage_gib         INT UNSIGNED   NOT NULL,
    launch_time         DATETIME(6)    NULL,
    updated_at          DATETIME(6)    NOT NULL,
    INDEX idx_ec2_instances_type (instance_type),
    CONSTRAINT fk_ec2_instances_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE ec2_volumes (
    resource_ref          BIGINT UNSIGNED NOT NULL,
    volume_id             VARCHAR(32)  NOT NULL,
    device                VARCHAR(32)  NOT NULL,
    size_gib              INT UNSIGNED NULL,
    volume_type           VARCHAR(16)  NULL,
    iops                  INT UNSIGNED NULL,
    throughput_mibps      INT UNSIGNED NULL,
    encrypted             BOOLEAN      NULL,
    delete_on_termination BOOLEAN      NOT NULL,
    PRIMARY KEY (resource_ref, volume_id),
    CONSTRAINT fk_ec2_volumes_resource FOREIGN KEY (resource_ref) REFERENCES resources (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
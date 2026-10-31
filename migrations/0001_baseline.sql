-- Baseline: the exact schema of the live database at user_version 0, captured from a dev copy.
-- An existing database is adopted at this version with no structural change.

CREATE TABLE models (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT,
                description TEXT,
                published_at TEXT,
                designer_id INTEGER,
                designer_name TEXT,
                designer_username TEXT,
                primary_category TEXT,
                parent_category TEXT,
                pre_supported BOOLEAN DEFAULT 0,
                featured BOOLEAN DEFAULT 0,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                price_value REAL,
                price_currency TEXT,
                primary_image_url TEXT,
                raw_backup_path TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            , release_month TEXT, is_bonus BOOLEAN DEFAULT 0, vault_path TEXT, archive_download_url TEXT, source TEXT DEFAULT 'mmf');

CREATE TABLE model_tags (
                model_id INTEGER,
                tag TEXT NOT NULL,
                PRIMARY KEY (model_id, tag),
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );

CREATE TABLE model_files (
                file_id INTEGER PRIMARY KEY,
                model_id INTEGER NOT NULL,
                filename TEXT,
                size_bytes INTEGER,
                download_url TEXT,
                created_at TEXT,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );

CREATE TABLE schema_anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                anomaly_type TEXT NOT NULL,
                details TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE download_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                total_files INTEGER NOT NULL DEFAULT 0,
                downloaded_files INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP, destination_path TEXT,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );

CREATE TABLE downloaded_files (
                file_id INTEGER PRIMARY KEY,
                model_id INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                cleaned_filename TEXT NOT NULL,
                local_relative_path TEXT NOT NULL,
                expected_bytes INTEGER,
                actual_bytes INTEGER NOT NULL,
                has_size_prefix BOOLEAN DEFAULT 0,
                has_size_suffix BOOLEAN DEFAULT 0,
                file_extension TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, storage_tier TEXT NOT NULL DEFAULT 'local', staging_path TEXT, vault_path TEXT, sha256 TEXT,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );

CREATE TABLE download_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                file_id INTEGER,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                error_detail TEXT,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE releases (
                id TEXT PRIMARY KEY,
                designer_name TEXT NOT NULL,
                designer_id INTEGER,
                release_month TEXT NOT NULL,
                model_count INTEGER NOT NULL,
                bonus_count INTEGER NOT NULL DEFAULT 0,
                total_files INTEGER NOT NULL,
                total_size_bytes INTEGER NOT NULL,
                is_likely_bundle BOOLEAN DEFAULT 0,
                sample_image_url TEXT,
                common_tags TEXT,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE pipeline_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                progress_pct REAL DEFAULT 0.0,
                progress_label TEXT,
                log_output TEXT DEFAULT '',
                result_summary_json TEXT,
                artifacts_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            , params_json TEXT, result_json TEXT, error_message TEXT);

CREATE TABLE reconciliation_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL,
                        progress_pct REAL DEFAULT 0,
                        current_item TEXT,
                        total_audited INTEGER DEFAULT 0,
                        healthy_count INTEGER DEFAULT 0,
                        discrepancy_count INTEGER DEFAULT 0,
                        report_json TEXT,
                        error_message TEXT,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP
                    , scan_mode TEXT DEFAULT 'fast');

CREATE TABLE file_archive_submodels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL,
                        model_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        relative_dir TEXT,
                        preview_image_path TEXT,
                        total_files INTEGER DEFAULT 0,
                        total_bytes INTEGER DEFAULT 0,
                        has_presupported BOOLEAN DEFAULT 0,
                        has_unsupported BOOLEAN DEFAULT 0,
                        has_slicer_project BOOLEAN DEFAULT 0,
                        formats_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, scales_json TEXT DEFAULT '[]',
                        FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
                        FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                    );

CREATE TABLE file_archive_members (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        submodel_id INTEGER,
                        file_id INTEGER NOT NULL,
                        internal_path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size_bytes INTEGER DEFAULT 0,
                        is_supported BOOLEAN DEFAULT 0,
                        file_extension TEXT,
                        FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE SET NULL,
                        FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE
                    );

CREATE TABLE stashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT 'A pile of gray',
                is_active BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE stash_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stash_id INTEGER NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('model', 'file', 'submodel', 'archive_member')),
                model_id INTEGER NOT NULL,
                file_id INTEGER,
                submodel_id INTEGER,
                archive_member_id INTEGER,
                notes TEXT DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (stash_id) REFERENCES stashes(id) ON DELETE CASCADE,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
                FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE CASCADE,
                FOREIGN KEY (archive_member_id) REFERENCES file_archive_members(id) ON DELETE CASCADE
            );

CREATE TABLE external_libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT UNIQUE NOT NULL,
                default_creator TEXT,
                disk_bytes INTEGER DEFAULT 0,
                total_models INTEGER DEFAULT 0,
                total_files INTEGER DEFAULT 0,
                last_scanned_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

CREATE INDEX idx_models_designer ON models(designer_name);

CREATE INDEX idx_models_category ON models(primary_category);

CREATE INDEX idx_tags_tag ON model_tags(tag);

CREATE INDEX idx_files_model ON model_files(model_id);

CREATE INDEX idx_models_release_month ON models(release_month);

CREATE INDEX idx_models_bonus ON models(is_bonus);

CREATE INDEX idx_jobs_status ON download_jobs(status);

CREATE INDEX idx_downloaded_model ON downloaded_files(model_id);

CREATE INDEX idx_audit_model ON download_audit_log(model_id);

CREATE INDEX idx_downloaded_tier ON downloaded_files(storage_tier);

CREATE INDEX idx_releases_designer ON releases(designer_name);

CREATE INDEX idx_releases_month ON releases(release_month);

CREATE INDEX idx_pipeline_tasks_status ON pipeline_tasks(status);

CREATE INDEX idx_archive_submodels_file ON file_archive_submodels(file_id);

CREATE INDEX idx_archive_submodels_model ON file_archive_submodels(model_id);

CREATE INDEX idx_archive_members_file ON file_archive_members(file_id);

CREATE INDEX idx_archive_members_submodel ON file_archive_members(submodel_id);

CREATE INDEX idx_stashes_active ON stashes(is_active);

CREATE INDEX idx_stash_items_stash ON stash_items(stash_id);

CREATE INDEX idx_stash_items_model ON stash_items(model_id);

CREATE INDEX idx_stash_items_type ON stash_items(stash_id, item_type);

CREATE UNIQUE INDEX idx_stash_items_unique ON stash_items(
                stash_id,
                item_type,
                model_id,
                COALESCE(file_id, 0),
                COALESCE(submodel_id, 0),
                COALESCE(archive_member_id, 0)
            );

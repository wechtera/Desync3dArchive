"""Raw SQL for the storage API routes. Names with braces take format() values, never user input."""

POST_STORAGE_CONFIG_1 = """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """

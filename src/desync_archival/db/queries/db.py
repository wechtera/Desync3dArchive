"""Raw SQL for services/db.py. Names with braces take format() values."""

INSERT_APP_SETTINGS = """
            INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)
        """

SELECT_APP_SETTINGS_VALUE_BY_KEY = "SELECT value FROM app_settings WHERE key = 'cookies_json';"

UPDATE_APP_SETTINGS_VALUE_BY_KEY = "UPDATE app_settings SET value = ? WHERE key = 'cookies_json';"

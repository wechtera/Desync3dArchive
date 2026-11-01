"""Raw SQL for services/secret_store.py. Names with braces take format() values."""

UPDATE_APP_SETTINGS_VALUE_BY_KEY = "UPDATE app_settings SET value = '' WHERE key = ?;"

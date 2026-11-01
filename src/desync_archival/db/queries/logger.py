"""Raw SQL for services/logger.py. Names with braces take format() values."""

SELECT_APP_SETTINGS_BY_KEY = "SELECT key, value FROM app_settings WHERE key IN ('log_level', 'log_layers');"

"""
services/logger.py - Centralized rotating file logger with dynamic subsystem layer filtering.
"""

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
from desync_archival import settings as app_config
import sqlite3
import threading
from typing import Any, Dict, List, Set

LOGS_DIR = app_config.logs_dir()
LOGS_DIR.mkdir(parents=True, exist_ok=True)

APP_LOG_PATH = LOGS_DIR / "vault.log"

# Supported diagnostic subsystem layers
VALID_LAYERS = {"http", "storage", "archive", "sql"}

# In-memory circular buffer for live UI log streaming (max 1,000 lines)
_UI_LOG_BUFFER = deque(maxlen=1000)
_BUFFER_LOCK = threading.Lock()
_CONFIG_LOCK = threading.Lock()

# Dynamic runtime configuration (defaults)
_ACTIVE_LEVEL = logging.INFO
_ACTIVE_LAYERS: Set[str] = set()

# Correlation IDs. Threads start with an empty context, so workers must bind their own.
_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_JOB_ID: ContextVar[str | None] = ContextVar("job_id", default=None)

REDACTED = "[REDACTED]"
_SECRET_NAMES = (
    r"access_token|refresh_token|id_token|client_secret|api_key|password|authorization|"
    r"cookies_json|cookie_cf_clearance|cookie_phpsessid|cf_clearance|phpsessid|mmf_access_token|"
    r"mmf_refresh_token|mmf_client_secret|token|secret|set-cookie|cookie"
)
_SIGNED_QUERY_NAMES = (
    r"x-amz-[a-z0-9-]+|signature|sig|token|access_token|expires|policy|key-pair-id|"
    r"awsaccesskeyid|googleaccessid"
)
_REDACTION_RULES = (
    # Authorization: Bearer abc
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), rf"\1 {REDACTED}"),
    # "name": "value" and 'name': 'value', including JSON-encoded values
    (
        re.compile(rf"""(["'])({_SECRET_NAMES})\1(\s*:\s*)(["'])(?:\\.|(?!\4).)*\4""", re.IGNORECASE),
        rf"\1\2\1\3\4{REDACTED}\4",
    ),
    # name=value and name: value, up to a separator. Includes Cookie headers.
    (
        re.compile(rf"\b({_SECRET_NAMES})(\s*[=:]\s*)(?![\[\"'{{])[^\s&;,'\"]+", re.IGNORECASE),
        rf"\1\2{REDACTED}",
    ),
    # Signed URL query values
    (
        re.compile(rf"([?&](?:{_SIGNED_QUERY_NAMES})=)[^&\s'\"]+", re.IGNORECASE),
        rf"\1{REDACTED}",
    ),
    # cookie: a=1; b=2 leaves later pairs behind the first redaction
    (re.compile(r"(\[REDACTED\])(?:;\s*[\w.-]+=[^;\s'\"]+)+"), r"\1"),
)


def redact(text: str) -> str:
    """Removes tokens, cookies and signed URL values from text."""
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


@contextmanager
def log_context(request_id: str | None = None, job_id: str | None = None) -> Iterator[None]:
    """Binds correlation IDs to every log line emitted inside the block."""
    tokens = []
    if request_id is not None:
        tokens.append((_REQUEST_ID, _REQUEST_ID.set(request_id)))
    if job_id is not None:
        tokens.append((_JOB_ID, _JOB_ID.set(job_id)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def job_logged(job_id_for: Callable[..., Any]) -> Callable:
    """Decorates a worker so its log lines carry the job ID built from its arguments."""
    def decorator(function: Callable) -> Callable:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with log_context(job_id=str(job_id_for(*args, **kwargs))):
                return function(*args, **kwargs)
        return wrapper
    return decorator


class UILogHandler(logging.Handler):
    """Pushes formatted records to an in-memory circular buffer for the web UI."""
    def emit(self, record):
        try:
            msg = self.format(record)
            with _BUFFER_LOCK:
                _UI_LOG_BUFFER.append(msg)
        except Exception:
            self.handleError(record)


def get_ui_logs(tail: int = 200) -> List[str]:
    """Returns the latest log lines from the in-memory ring buffer."""
    with _BUFFER_LOCK:
        if tail >= len(_UI_LOG_BUFFER):
            return list(_UI_LOG_BUFFER)
        return list(_UI_LOG_BUFFER)[-tail:]


def clear_ui_logs():
    """Clears the live console buffer."""
    with _BUFFER_LOCK:
        _UI_LOG_BUFFER.clear()


class LayerFilter(logging.Filter):
    """Filters log records based on the active log level and subsystem layer."""
    def filter(self, record: logging.LogRecord) -> bool:
        layer = getattr(record, "layer", None)
        # If the record belongs to a specific layer and that layer is disabled, suppress it
        if layer and layer in VALID_LAYERS:
            with _CONFIG_LOCK:
                if layer not in _ACTIVE_LAYERS:
                    return False
        return True


class RedactionFilter(logging.Filter):
    """Redacts each record once, before any handler sees it."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class JsonLinesFormatter(logging.Formatter):
    """Writes each record as one JSON object."""
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "thread": record.threadName,
            "message": record.getMessage(),
            "layer": getattr(record, "layer", None),
            "request_id": _REQUEST_ID.get(),
            "job_id": _JOB_ID.get(),
        }
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False)


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("vault")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)  # Root level stays DEBUG; handlers filter
    logger.addFilter(LayerFilter())
    logger.addFilter(RedactionFilter())

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Rotating File Handler (10 MB * 4 files = 40 MB max disk usage)
    file_handler = RotatingFileHandler(
        APP_LOG_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonLinesFormatter())
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    # 2. In-Memory UI Handler
    ui_handler = UILogHandler()
    ui_handler.setFormatter(formatter)
    ui_handler.setLevel(logging.DEBUG)
    logger.addHandler(ui_handler)

    # 3. Standard Console stdout
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


# -----------------------------------------------------------------------------
# Dynamic Configuration Helpers
# -----------------------------------------------------------------------------

def set_logging_config(level_name: str, enabled_layers: List[str]):
    """Dynamically updates active log levels and enabled subsystem layers."""
    global _ACTIVE_LEVEL, _ACTIVE_LAYERS

    lvl = getattr(logging, level_name.upper(), logging.INFO)
    clean_layers = {l.strip().lower() for l in enabled_layers if l.strip().lower() in VALID_LAYERS}

    with _CONFIG_LOCK:
        _ACTIVE_LEVEL = lvl
        _ACTIVE_LAYERS = clean_layers
        for handler in logger.handlers:
            handler.setLevel(_ACTIVE_LEVEL)

    logger.info(f"[Config] Updated logging: Level={logging.getLevelName(lvl)}, Active Layers={sorted(clean_layers)}")


def get_logging_config() -> Dict[str, Any]:
    """Returns the current logging configuration."""
    with _CONFIG_LOCK:
        return {
            "level": logging.getLevelName(_ACTIVE_LEVEL),
            "layers": sorted(list(_ACTIVE_LAYERS)),
            "available_layers": sorted(list(VALID_LAYERS)),
        }


def init_logging_from_db(db_path: Path):
    """Initializes logging settings from the SQLite database."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT key, value FROM app_settings WHERE key IN ('log_level', 'log_layers');").fetchall()
            settings = {r["key"]: r["value"] for r in rows}

            level = settings.get("log_level", "INFO")
            layers_raw = settings.get("log_layers", "")
            layers = [l.strip() for l in layers_raw.split(",") if l.strip()]

            set_logging_config(level, layers)
    except Exception as e:
        logger.warning(f"Could not load logging configuration from database: {e}")


# -----------------------------------------------------------------------------
# Subsystem Layer Logging Dispatcher
# -----------------------------------------------------------------------------

def log_layer(layer: str, level: int, message: str, **kwargs):
    """
    Emits a log entry tagged with a specific subsystem layer.
    Aborts immediately if the subsystem layer is not enabled.
    """
    clean_layer = layer.strip().lower()
    with _CONFIG_LOCK:
        if clean_layer in VALID_LAYERS and clean_layer not in _ACTIVE_LAYERS:
            return

    extra = kwargs.get("extra", {})
    extra["layer"] = clean_layer
    kwargs["extra"] = extra

    tag = f"[{clean_layer.upper()}] "
    logger.log(level, f"{tag}{message}", **kwargs)

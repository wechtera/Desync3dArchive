"""One source for where the app keeps its data, and the guard that protects production.

The data directory has no default, so the app can't fall back to a path next to the code.
Outside APP_ENV=production, the guard refuses any path that resolves to a production location.
"""

import ipaddress
import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = "production"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_BODY_BYTES = 2 * 1024 * 1024
# Stored settings that hold filesystem paths.
PATH_SETTING_KEYS = ("download_root_path", "download_work_path", "stash_download_path", "metadata_backup_path")


class SettingsError(RuntimeError):
    """The configuration is missing or invalid, so the app must not start."""


class ProductionPathError(SettingsError):
    """A non-production run reached for a production location."""


class PathNotAllowedError(SettingsError):
    """A path is outside DESYNC_ALLOWED_ROOTS."""


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@lru_cache(maxsize=1)
def _load() -> None:
    _load_env_file(REPO_ROOT / ".env")


def app_env() -> str:
    _load()
    value = os.environ.get("APP_ENV", "").strip()
    if not value:
        raise SettingsError("APP_ENV is required. See .env.example.")
    return value


def is_production() -> bool:
    return app_env() == PRODUCTION


def host() -> str:
    _load()
    return os.environ.get("DESYNC_HOST", "").strip() or DEFAULT_HOST


def port() -> int:
    _load()
    value = os.environ.get("DESYNC_PORT", "").strip()
    try:
        return int(value) if value else DEFAULT_PORT
    except ValueError as e:
        raise SettingsError("DESYNC_PORT must be a number.") from e


def is_loopback(bind_host: str) -> bool:
    if bind_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind_host).is_loopback
    except ValueError:
        return False


def login_required() -> bool:
    """Login is required when reachable from other machines, or when forced on."""
    _load()
    return not is_loopback(host()) or os.environ.get("DESYNC_REQUIRE_LOGIN", "") == "1"


def secrets_file() -> Path:
    _load()
    value = os.environ.get("DESYNC_SECRETS_FILE", "").strip()
    return Path(value).expanduser().resolve() if value else Path.home() / ".config" / "desync-archival" / "secrets.json"


def tls_files() -> tuple[Path, Path] | None:
    _load()
    cert = os.environ.get("DESYNC_TLS_CERT", "").strip()
    key = os.environ.get("DESYNC_TLS_KEY", "").strip()
    return (Path(cert).expanduser(), Path(key).expanduser()) if cert and key else None


def allowed_hosts() -> set[str]:
    _load()
    extra = os.environ.get("DESYNC_ALLOWED_HOSTS", "")
    return set(DEFAULT_ALLOWED_HOSTS) | {item.strip().lower() for item in extra.split(",") if item.strip()}


def protected_paths() -> list[Path]:
    _load()
    paths = [REPO_ROOT.parent / "scrapepage", Path.home() / "DESYNC_Data"]
    configured = os.environ.get("DESYNC_PROTECTED_PATHS", "")
    paths.extend(Path(item).expanduser() for item in configured.split(":") if item.strip())
    return [path.resolve() for path in paths]


def is_protected(path: Path | str) -> bool:
    """Compares whole path components, so ~/DESYNC_Data_dev does not match ~/DESYNC_Data."""
    resolved = Path(path).expanduser().resolve()
    return any(resolved == item or item in resolved.parents for item in protected_paths())


def check_path(path: Path | str) -> None:
    if not is_production() and is_protected(path):
        raise ProductionPathError(
            f"Refusing production path outside APP_ENV={PRODUCTION}: {Path(path).expanduser().resolve()}"
        )


def allowed_roots() -> list[Path]:
    """The folders the API can browse or set paths in. Only the server's environment sets these."""
    _load()
    configured = os.environ.get("DESYNC_ALLOWED_ROOTS", "")
    roots = [Path(item).expanduser().resolve() for item in configured.split(":") if item.strip()]
    return roots or [data_dir()]


def is_allowed(path: Path | str) -> bool:
    resolved = Path(path).expanduser().resolve()
    return any(resolved == root or root in resolved.parents for root in allowed_roots())


def check_settable_path(path: Path | str) -> None:
    """Refuses a path from an API request that production rules or the allowed roots rule out."""
    check_path(path)
    if not is_allowed(path):
        raise PathNotAllowedError("That path is outside the allowed roots. See DESYNC_ALLOWED_ROOTS.")


def _raw_data_dir() -> Path:
    _load()
    value = os.environ.get("DESYNC_DATA_DIR", "").strip()
    if not value:
        raise SettingsError("DESYNC_DATA_DIR is required. See .env.example.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SettingsError("DESYNC_DATA_DIR must be an absolute path.")
    return path.resolve()


def data_dir() -> Path:
    path = _raw_data_dir()
    check_path(path)
    if app_env() == "development" and not os.environ.get("DESYNC_PROTECTED_PATHS", "").strip():
        raise SettingsError("DESYNC_PROTECTED_PATHS is required in development. See .env.example.")
    return path


def db_path() -> Path:
    return data_dir() / "library.db"


def previews_dir() -> Path:
    return data_dir() / "previews"


def metadata_dir() -> Path:
    return data_dir() / "metadata"


def logs_dir() -> Path:
    return data_dir() / "logs"


def scrapes_dir() -> Path:
    return data_dir() / "scrapes"


def staging_dir() -> Path:
    return data_dir() / "staging"


def backup_dir() -> Path:
    _load()
    value = os.environ.get("DESYNC_BACKUP_DIR", "").strip()
    if not value:
        raise SettingsError("DESYNC_BACKUP_DIR is required for backups. See .env.example.")
    return Path(value).expanduser().resolve()


def web_dir() -> Path:
    return REPO_ROOT / "web"


def scraper_script() -> Path:
    return REPO_ROOT / "scraper" / "scrape.js"


def blender_script() -> Path:
    return REPO_ROOT / "scripts" / "blender_render_mesh.py"


def resolve_data_path(value: Path | str) -> Path:
    """Relative stored paths belong to the data directory, not the working directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else data_dir() / path


def check_stored_paths(conn) -> None:
    """Checks paths stored in the database, which environment settings can't see."""
    marks = ",".join("?" * len(PATH_SETTING_KEYS))
    rows = conn.execute(f"SELECT value FROM app_settings WHERE key IN ({marks})", PATH_SETTING_KEYS).fetchall()
    stored = [row[0] for row in rows if row[0]]
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'external_libraries'").fetchone()
    if table:
        stored.extend(row[0] for row in conn.execute("SELECT path FROM external_libraries WHERE is_active = 1").fetchall() if row[0])
    for value in stored:
        check_path(resolve_data_path(value))

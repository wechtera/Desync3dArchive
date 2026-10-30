"""Normalizing, redacting and scanning for API contract snapshots."""

import re
import urllib.parse
from typing import Any

REDACTED = "<REDACTED>"
TIMESTAMP = "<TIMESTAMP>"
VOLATILE = "<VOLATILE>"

SECRET_KEY_PATTERN = re.compile(
    r"(^|_)(token|secret|password|cookie|cookies|cookies_json|phpsessid|cf_clearance|"
    r"client_id|api_key|oauth_state|username|user_id)$",
    re.IGNORECASE,
)
# Status fields that describe a secret without holding it.
NOT_SECRET_KEY_PATTERN = re.compile(r"^(has|is)_|_(configured|present|set)$", re.IGNORECASE)

SIGNED_QUERY_PATTERN = re.compile(
    r"^(x-amz-.*|signature|sig|token|access_token|expires|policy|key-pair-id|awsaccesskeyid)$",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
TIMESTAMP_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
SCRAPE_STAMP_PATTERN = re.compile(r"\b\d{8}[_-]\d{6}\b")
INLINE_SECRET_PATTERN = re.compile(
    r"(cf_clearance|PHPSESSID|access_token|client_secret|refresh_token)=[A-Za-z0-9._%-]{8,}"
)
BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}")

VOLATILE_KEYS = {
    "free_bytes",
    "used_bytes",
    "total_bytes",
    "free_gb",
    "used_gb",
    "total_gb",
    "percent_used",
    "usage_percent",
    "disk_free",
    "disk_total",
    "disk_used",
    "duration_ms",
    "duration_seconds",
    "elapsed_seconds",
    "uptime_seconds",
    "started_at",
    "finished_at",
    "completed_at",
    "created_at",
    "updated_at",
    "computed_at",
    "last_scanned_at",
    "expires_at",
    "state",
}


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_PATTERN.search(key)) and not NOT_SECRET_KEY_PATTERN.search(key)


def redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(SIGNED_QUERY_PATTERN.match(key) for key, _ in pairs):
        return url
    kept = [
        (key, REDACTED if SIGNED_QUERY_PATTERN.match(key) else value)
        for key, value in pairs
    ]
    query = "&".join(f"{key}={value}" for key, value in kept)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def normalize_text(text: str, replacements: list[tuple[str, str]]) -> str:
    for old, new in replacements:
        text = text.replace(old, new)
    text = URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    text = INLINE_SECRET_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    text = BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)
    text = TIMESTAMP_PATTERN.sub(TIMESTAMP, text)
    return SCRAPE_STAMP_PATTERN.sub(TIMESTAMP, text)


def normalize(value: Any, replacements: list[tuple[str, str]], key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: normalize(v, replacements, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(item, replacements, key) for item in value]
    if key in VOLATILE_KEYS and value is not None:
        return VOLATILE
    if is_secret_key(key) and value not in (None, "", 0, False, True):
        return REDACTED
    if isinstance(value, str):
        return normalize_text(value, replacements)
    return value


def find_leaks(value: Any, key: str = "", location: str = "$") -> list[str]:
    """Returns where a parsed snapshot holds a secret or a signed URL, without saying what it is."""
    leaks: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            leaks.extend(find_leaks(v, str(k), f"{location}.{k}"))
        return leaks
    if isinstance(value, list):
        for index, item in enumerate(value):
            leaks.extend(find_leaks(item, key, f"{location}[{index}]"))
        return leaks
    if is_secret_key(key) and value not in (None, "", 0, False, True, REDACTED):
        leaks.append(f"{location}: value under a secret key")
    if isinstance(value, str):
        if INLINE_SECRET_PATTERN.search(value) or BEARER_PATTERN.search(value):
            leaks.append(f"{location}: inline secret")
        for match in URL_PATTERN.finditer(value):
            pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(match.group(0)).query, keep_blank_values=True)
            # URL_PATTERN stops at "<", so a redacted value reads back as blank here.
            if any(SIGNED_QUERY_PATTERN.match(k) and v not in ("", REDACTED) for k, v in pairs):
                leaks.append(f"{location}: signed URL")
    return leaks

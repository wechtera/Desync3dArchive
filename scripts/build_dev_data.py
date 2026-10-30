#!/usr/bin/env python3
"""Builds a sanitized development data directory from production.

The source database is opened read-only and copied with SQLite's backup API. Every change is made
to the copy, which only moves into place after it passes verification:

- Secrets are blanked and wiped from free pages, so they are not in the file at all.
- Production paths are rewritten to folders inside the destination.
- A small sample of previews and metadata is copied.
- The old app's code is copied to old_app/, with no cookies, browser profile or scrape output.
"""

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger("build_dev_data")

MARKER_NAME = ".desync_dev_data"
KEPT_ON_REBUILD = {"baseline", MARKER_NAME}
SAMPLE_SIZE = 25

SECRET_KEYS = (
    "PHPSESSID",
    "cf_clearance",
    "cookie_cf_clearance",
    "cookie_phpsessid",
    "cookies_json",
    "mmf_access_token",
    "mmf_refresh_token",
    "mmf_client_id",
    "mmf_client_secret",
    "mmf_oauth_state",
    "mmf_token_expires_at",
)
# Values too short or too generic to search the whole database for.
NOT_SWEPT_KEYS = {"mmf_token_expires_at"}
MIN_SWEPT_LENGTH = 8

PATH_SETTINGS = {
    "download_root_path": "nas/vault",
    "download_work_path": "staging",
    "stash_download_path": "stash",
}
OLD_APP_ITEMS = ("services", "ui", "scripts")


class SanitizeError(Exception):
    """The dev copy failed verification, so it must not be used."""


def is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def text_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [
        (table, column[1])
        for (table,) in tables
        for column in conn.execute(f'PRAGMA table_info("{table}")')
    ]


def read_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {key: value or "" for key, value in conn.execute("SELECT key, value FROM app_settings")}


def copy_database(source_db: Path, target_db: Path) -> None:
    descriptor = os.open(target_db, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(descriptor)
    source = sqlite3.connect(f"{source_db.as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(target_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def build_path_map(conn: sqlite3.Connection, source_root: Path, dest: Path) -> dict[str, str]:
    settings = read_settings(conn)
    path_map = {str(source_root): str(dest)}
    for key, subdir in PATH_SETTINGS.items():
        if settings.get(key, "").startswith("/"):
            path_map[settings[key].rstrip("/")] = str(dest / subdir)
    for (name, path) in conn.execute("SELECT name, path FROM external_libraries"):
        path_map[path.rstrip("/")] = str(dest / "nas" / name.replace("/", "_"))
    return path_map


def sanitize_database(db_path: Path, source_root: Path, dest: Path) -> list[str]:
    """Sanitizes the copy and returns the secret values so the file can be checked for them."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        settings = read_settings(conn)
        secrets = [
            settings[key]
            for key in SECRET_KEYS
            if key not in NOT_SWEPT_KEYS and len(settings.get(key, "")) >= MIN_SWEPT_LENGTH
        ]
        path_map = build_path_map(conn, source_root, dest)
        columns = text_columns(conn)

        with conn:
            for key in SECRET_KEYS:
                conn.execute("UPDATE app_settings SET value = '' WHERE key = ?", (key,))
            for key, subdir in PATH_SETTINGS.items():
                conn.execute(
                    "UPDATE app_settings SET value = ? WHERE key = ?", (str(dest / subdir), key)
                )
            # Longest first, so a nested production path is not half-rewritten by its parent.
            replacements = sorted(path_map.items(), key=lambda item: len(item[0]), reverse=True)
            for old, new in replacements + [(secret, "") for secret in secrets]:
                for table, column in columns:
                    conn.execute(
                        f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?) '
                        f"WHERE typeof(\"{column}\") = 'text' AND instr(\"{column}\", ?) > 0",
                        (old, new, old),
                    )

        # VACUUM rewrites the file, which drops the free pages that still hold blanked values.
        conn.execute("VACUUM")
    finally:
        conn.close()
    return secrets


def verify_database(db_path: Path, secrets: list[str], forbidden_paths: list[str], dest: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        settings = read_settings(conn)
        for key in SECRET_KEYS:
            if settings.get(key):
                raise SanitizeError(f"Secret setting {key} is still set")
        for key in PATH_SETTINGS:
            if key in settings and not is_within(Path(settings[key]), dest):
                raise SanitizeError(f"Path setting {key} is outside the destination")

        for table, column in text_columns(conn):
            for path in forbidden_paths:
                # The destination can sit under a forbidden prefix, such as the home directory.
                count = conn.execute(
                    f'SELECT count(*) FROM "{table}" '
                    f"WHERE instr(replace(\"{column}\", ?, ''), ?) > 0",
                    (str(dest), path),
                ).fetchone()[0]
                if count:
                    raise SanitizeError(f"{count} rows in {table}.{column} still hold a production path")
            for secret in secrets:
                count = conn.execute(
                    f'SELECT count(*) FROM "{table}" WHERE instr("{column}", ?) > 0', (secret,)
                ).fetchone()[0]
                if count:
                    raise SanitizeError(f"{count} rows in {table}.{column} still hold a secret")
    finally:
        conn.close()

    raw = db_path.read_bytes()
    for secret in secrets:
        if secret.encode("utf-8") in raw:
            raise SanitizeError("A secret value is still in the database file bytes")


def sample_model_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id FROM (
                SELECT id FROM models WHERE vault_path IS NOT NULL ORDER BY id LIMIT ?
            )
            UNION
            SELECT id FROM (SELECT id FROM models ORDER BY id LIMIT ?)
            UNION
            SELECT id FROM (SELECT id FROM models WHERE source = 'external' ORDER BY id LIMIT 5)
            """,
            (SAMPLE_SIZE, SAMPLE_SIZE),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def copy_sample(source_root: Path, target_root: Path, model_ids: list[int]) -> None:
    (target_root / "previews").mkdir(parents=True, exist_ok=True)
    (target_root / "metadata").mkdir(parents=True, exist_ok=True)
    for model_id in model_ids:
        preview = source_root / "previews" / str(model_id)
        if preview.is_dir() and not preview.is_symlink():
            shutil.copytree(preview, target_root / "previews" / str(model_id), symlinks=True)
        metadata = source_root / "metadata" / f"model_{model_id}.json"
        if metadata.is_file() and not metadata.is_symlink():
            shutil.copy2(metadata, target_root / "metadata" / metadata.name)


def copy_old_app(source_root: Path, dest: Path) -> None:
    old_app = dest / "old_app"
    old_app.mkdir()
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*cookies*", "*.db", "*.log")
    for item in OLD_APP_ITEMS:
        shutil.copytree(source_root / item, old_app / item, ignore=ignored)


def clear_previous_build(dest: Path) -> None:
    for child in dest.iterdir():
        if child.name in KEPT_ON_REBUILD:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def check_destination(source_root: Path, dest: Path, protected: list[Path], repo_root: Path) -> None:
    if is_within(dest, source_root) or is_within(source_root, dest):
        raise SanitizeError("The destination and the source must not overlap")
    if is_within(dest, repo_root):
        raise SanitizeError("The destination must be outside the repository")
    for path in protected:
        if is_within(dest, path) or is_within(path, dest):
            raise SanitizeError(f"The destination overlaps a protected path: {path}")
    if dest.exists() and any(dest.iterdir()) and not (dest / MARKER_NAME).exists():
        only_baseline = {child.name for child in dest.iterdir()} == {"baseline"}
        if not only_baseline:
            raise SanitizeError(f"{dest} has content but no {MARKER_NAME} marker. Refusing to touch it")


def build(
    source_root: Path,
    dest: Path,
    protected: list[Path],
    repo_root: Path,
    forbidden_paths: list[str],
) -> None:
    check_destination(source_root, dest, protected, repo_root)
    source_db = source_root / "library.db"
    if not source_db.is_file():
        raise SanitizeError(f"No database at {source_db}")

    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    (dest / MARKER_NAME).touch()
    clear_previous_build(dest)

    temp_db = dest / "library.db.building"
    try:
        logger.info("Copying database with the backup API")
        copy_database(source_db, temp_db)
        logger.info("Sanitizing the copy")
        secrets = sanitize_database(temp_db, source_root, dest)
        logger.info("Verifying the copy")
        verify_database(temp_db, secrets, forbidden_paths, dest)
    except BaseException:
        # This file can hold live secrets until verification passes.
        temp_db.unlink(missing_ok=True)
        raise

    final_db = dest / "library.db"
    temp_db.rename(final_db)
    for subdir in ("nas/vault", "staging", "stash", "scrapes"):
        (dest / subdir).mkdir(parents=True, exist_ok=True)

    model_ids = sample_model_ids(final_db)
    copy_sample(source_root, dest, model_ids)
    copy_old_app(source_root, dest)
    copy_sample(source_root, dest / "old_app", model_ids)
    shutil.copy2(final_db, dest / "old_app" / "library.db")
    logger.info("Built %s with %d sampled models", dest, len(model_ids))


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=repo_root.parent / "scrapepage")
    parser.add_argument("--dest", type=Path, default=Path.home() / "DESYNC_Data_dev")
    parser.add_argument(
        "--protect",
        type=Path,
        action="append",
        default=[],
        help="Extra production path that the destination must not overlap",
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="Extra text that must not remain in the copy, such as a mount point",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    forbidden_paths = [str(Path.home()), "/Volumes/", *args.forbid]
    protected = [Path.home() / "DESYNC_Data", *[path.resolve() for path in args.protect]]
    try:
        build(args.source.resolve(), args.dest.resolve(), protected, repo_root, forbidden_paths)
    except SanitizeError as e:
        logger.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

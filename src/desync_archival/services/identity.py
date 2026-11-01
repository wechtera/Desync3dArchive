"""Maps a source's own IDs to catalog rows.

Internal keys mean nothing outside this database. Old rows happen to share a number with their MMF
ID, so code must always go through external_key to talk to MMF, and never assume the two match.
"""

import sqlite3
from collections.abc import Iterable

MMF = "mmf"

MODEL_COLUMNS = (
    "name", "url", "description", "published_at", "designer_id", "designer_name", "designer_username",
    "primary_category", "parent_category", "pre_supported", "featured", "views", "likes", "price_value",
    "price_currency", "primary_image_url", "raw_backup_path",
)


def find_model(conn: sqlite3.Connection, source: str, external_key: object) -> int | None:
    row = conn.execute(
        "SELECT id FROM collections WHERE source = ? AND external_key = ?;", (source, str(external_key))
    ).fetchone()
    return row[0] if row else None


def mmf_key(conn: sqlite3.Connection, model_id: int) -> str | None:
    """The ID MMF knows a model by, or None for anything that did not come from MMF."""
    row = conn.execute("SELECT external_key FROM collections WHERE id = ? AND source = ?;", (model_id, MMF)).fetchone()
    return row[0] if row else None


def archive_file_key(mmf_id: object) -> str:
    """Key for a bundle with no file list of its own. MMF gives it no ID, so it derives from the model."""
    return str(int(str(mmf_id)) * 1000 + 1)


def save_mmf_model(conn: sqlite3.Connection, mmf_id: object, values: tuple) -> int:
    """Updates the model MMF knows by this ID, or adds it, and returns the internal key."""
    assignments = ", ".join(f"{column} = ?" for column in MODEL_COLUMNS)
    updated = conn.execute(
        f"UPDATE collections SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE source = ? AND external_key = ?;",
        (*values, MMF, str(mmf_id)),
    )
    if updated.rowcount:
        return conn.execute(
            "SELECT id FROM collections WHERE source = ? AND external_key = ?;", (MMF, str(mmf_id))
        ).fetchone()[0]

    columns = ", ".join(MODEL_COLUMNS)
    marks = ", ".join("?" for _ in MODEL_COLUMNS)
    cursor = conn.execute(
        f"INSERT INTO collections ({columns}, source, external_key, updated_at) VALUES ({marks}, ?, ?, CURRENT_TIMESTAMP);",
        (*values, MMF, str(mmf_id)),
    )
    return int(cursor.lastrowid or 0)


def save_mmf_files(conn: sqlite3.Connection, model_id: int, file_rows: Iterable[tuple]) -> None:
    """Updates files in place so their keys survive a re-ingest. rows are (key, filename, size, url, created).

    A file MMF no longer lists is removed, unless it was downloaded, because the archive still has it.
    """
    kept = set()
    for key, filename, size_bytes, download_url, created_at in file_rows:
        key = str(key)
        kept.add(key)
        updated = conn.execute("""
            UPDATE model_files SET model_id = ?, filename = ?, size_bytes = ?, download_url = ?, created_at = ?
            WHERE source = ? AND external_key = ?;
        """, (model_id, filename, size_bytes, download_url, created_at, MMF, key))
        if not updated.rowcount:
            conn.execute("""
                INSERT INTO model_files (model_id, filename, size_bytes, download_url, created_at, source, external_key)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (model_id, filename, size_bytes, download_url, created_at, MMF, key))

    for file_id, key in conn.execute(
        "SELECT file_id, external_key FROM model_files WHERE model_id = ?;", (model_id,)
    ).fetchall():
        downloaded = conn.execute("SELECT 1 FROM downloaded_files WHERE file_id = ?;", (file_id,)).fetchone()
        if key not in kept and not downloaded:
            conn.execute("DELETE FROM model_files WHERE file_id = ?;", (file_id,))

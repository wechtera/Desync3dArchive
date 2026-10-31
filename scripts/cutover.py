#!/usr/bin/env python3
"""Staged migration rehearsal and rollback. Each stage stops and writes a report to read.

Order: preflight, then sample, then full, then rollback. Stages work on copies inside --workdir, and
the source database is only ever opened read-only. Only rollback changes a live data folder, and it
needs --confirm.
"""

import argparse
import http.client
import json
import os
import random
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

SAMPLE_SIZE = 25
SEARCH_TERMS = 200
EXEMPT_FROM_KEY_RULE = {"app_settings"}
VISIBLE = """
    is_active = 1 AND name NOT LIKE '[Unavailable Model %]' AND designer_name IS NOT NULL
    AND designer_name NOT IN ('', 'Unknown Designer', 'Unknown')
"""


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def copy_database(source: Path, target: Path) -> None:
    from_db = open_read_only(source)
    to_db = sqlite3.connect(target)
    try:
        from_db.backup(to_db)
    finally:
        to_db.close()
        from_db.close()


def tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'catalog_search%'
        ORDER BY name
    """).fetchall()
    return [row[0] for row in rows]


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def preflight(source: Path) -> dict[str, Any]:
    """Checks for anything that would stop a migration, without changing the database."""
    conn = open_read_only(source)
    try:
        problems = []
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(f"integrity_check: {integrity}")

        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            by_table: dict[str, int] = {}
            for row in broken:
                by_table[row[0]] = by_table.get(row[0], 0) + 1
            problems.append(f"broken references: {by_table}")

        repeated = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT vault_path FROM models
                WHERE source = 'external' AND vault_path IS NOT NULL
                GROUP BY vault_path HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
        if repeated:
            problems.append(f"{repeated} linked library paths are used by more than one model")

        no_file = conn.execute("""
            SELECT COUNT(*) FROM downloaded_files d LEFT JOIN model_files f ON f.file_id = d.file_id
            WHERE f.file_id IS NULL
        """).fetchone()[0]
        if no_file:
            problems.append(f"{no_file} downloaded files have no model_files row")

        no_source = conn.execute("SELECT COUNT(*) FROM models WHERE source IS NULL").fetchone()[0]
        if no_source:
            problems.append(f"{no_source} models have no source")

        return {
            "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "row_counts": {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables(conn)},
            "problems": problems,
            "passed": not problems,
        }
    finally:
        conn.close()


def pick_sample(conn: sqlite3.Connection, size: int) -> list[int]:
    """Takes every awkward kind of row first, then fills up at random, so a small set still covers them."""
    groups = [
        "SELECT DISTINCT model_id FROM stash_items",
        "SELECT DISTINCT model_id FROM downloaded_files WHERE sha256 IS NOT NULL AND sha256 != '' LIMIT 3",
        "SELECT DISTINCT model_id FROM downloaded_files WHERE sha256 IS NULL OR sha256 = '' LIMIT 3",
        "SELECT DISTINCT model_id FROM file_archive_submodels LIMIT 3",
        "SELECT id FROM models WHERE source = 'external' LIMIT 5",
        "SELECT id FROM models WHERE name LIKE '[Unavailable Model %]' LIMIT 3",
        "SELECT id FROM models WHERE is_bonus = 1 LIMIT 2",
        "SELECT id FROM models WHERE id NOT IN (SELECT model_id FROM model_files) LIMIT 2",
        "SELECT model_id FROM download_jobs WHERE status != 'completed' LIMIT 2",
    ]
    chosen: list[int] = []
    for query in groups:
        for (model_id,) in conn.execute(query):
            if model_id not in chosen:
                chosen.append(model_id)

    rest = [row[0] for row in conn.execute("SELECT id FROM models ORDER BY id") if row[0] not in chosen]
    random.Random(7).shuffle(rest)
    return chosen + rest[:max(0, size - len(chosen))]


def reduce_to_sample(path: Path, size: int) -> list[int]:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        keep = pick_sample(conn, size)
        marks = ",".join("?" * len(keep))
        conn.execute(f"DELETE FROM models WHERE id NOT IN ({marks})", keep)
        for table in ("download_audit_log", "schema_anomalies"):
            conn.execute(f"DELETE FROM {table} WHERE model_id NOT IN ({marks})", keep)
        conn.commit()
        conn.execute("VACUUM")
        return keep
    finally:
        conn.close()


def rewrite_paths(path: Path, old_root: Path) -> int:
    """Makes stored paths under the old data folder relative, so they follow the data to its new home."""
    prefix = f"{old_root}/"
    conn = sqlite3.connect(path)
    try:
        updated = conn.execute(
            "UPDATE models SET raw_backup_path = substr(raw_backup_path, ?) WHERE substr(raw_backup_path, 1, ?) = ?",
            (len(prefix) + 1, len(prefix), prefix),
        ).rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


def rewrite_stored_paths(path: Path, old_root: Path) -> dict[str, Any]:
    """The rewrite on its own, for the real cutover. Protected paths need APP_ENV=production."""
    from desync_archival import settings

    settings.check_path(path)
    if not path.is_file():
        raise SystemExit(f"No database at {path}")
    return {"database": str(path), "old_root": str(old_root), "paths_rewritten": rewrite_paths(path, old_root), "passed": True}


def isolate_paths(path: Path, stage_dir: Path) -> dict[str, str]:
    """Points a throwaway copy's stored storage paths inside its stage, away from the real ones.

    Runs after verify, because the copy no longer matches the source once it has run. The folders are
    left uncreated, so the app treats them as offline mounts. Returns the original of each change.
    """
    from desync_archival import settings

    sandbox = stage_dir / "sandbox"
    changed: dict[str, str] = {}
    conn = sqlite3.connect(path)
    try:
        marks = ",".join("?" * len(settings.PATH_SETTING_KEYS))
        for key, value in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({marks})", settings.PATH_SETTING_KEYS
        ).fetchall():
            if value and Path(value).is_absolute():
                conn.execute("UPDATE app_settings SET value = ? WHERE key = ?", (str(sandbox / key), key))
                changed[key] = value
        for rowid, value in conn.execute("SELECT rowid, path FROM external_libraries").fetchall():
            if value:
                conn.execute("UPDATE external_libraries SET path = ? WHERE rowid = ?", (str(sandbox / f"library_{rowid}"), rowid))
                changed[f"external_library_{rowid}"] = value
        conn.commit()
        return changed
    finally:
        conn.close()


def verify(before_path: Path, after_path: Path, rows_only: bool = False, old_root: Path | None = None) -> dict[str, Any]:
    """Proves a migration kept every value, and that the result follows the key rules.

    rows_only is for a restored backup, which is meant to have the older schema. old_root allows
    only the expected change to stored paths under that folder.
    """
    before, after = open_read_only(before_path), open_read_only(after_path)
    problems: list[str] = []
    compared = {}
    try:
        for table in tables(before):
            if table not in tables(after):
                problems.append(f"{table} is missing after migration")
                continue
            old_columns, new_columns = columns(before, table), columns(after, table)
            shared = [c for c in old_columns if c in new_columns]
            renamed = table == "releases" and "release_key" in new_columns
            if renamed:
                shared = [c for c in shared if c != "id"]
            old_columns_sql = [f'"{c}"' for c in shared]
            if table == "models" and old_root:
                old_columns_sql[shared.index("raw_backup_path")] = f"replace(raw_backup_path, '{old_root}/', '')"
            old_sql = ", ".join(['id'] * renamed + old_columns_sql)
            new_sql = ", ".join(['release_key'] * renamed + [f'"{c}"' for c in shared])
            old_rows = sorted(map(repr, before.execute(f'SELECT {old_sql} FROM "{table}"')))
            new_rows = sorted(map(repr, after.execute(f'SELECT {new_sql} FROM "{table}"')))
            compared[table] = len(old_rows)
            if old_rows != new_rows:
                problems.append(f"{table}: {len(old_rows)} rows before, {len(new_rows)} after, or values changed")

        if after.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("integrity_check failed")
        broken = after.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            problems.append(f"{len(broken)} broken references")

        if rows_only:
            return {"rows_compared": compared, "problems": problems, "passed": not problems}

        for table in tables(after):
            if table in EXEMPT_FROM_KEY_RULE:
                continue
            sql = after.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            if "AUTOINCREMENT" not in sql:
                problems.append(f"{table} has no increment key")
            if "UNIQUE" in sql.upper() or any(row[2] for row in after.execute(f'PRAGMA index_list("{table}")')):
                problems.append(f"{table} has a unique constraint")

        wrong = after.execute("""
            SELECT COUNT(*) FROM models
            WHERE (source = 'mmf' AND external_key IS NOT CAST(id AS TEXT)) OR (source != 'mmf' AND external_key IS NOT NULL)
        """).fetchone()[0]
        if wrong:
            problems.append(f"{wrong} models have the wrong external_key")

        for table, key in (("models", "id"), ("model_files", "file_id")):
            highest = after.execute(f"SELECT COALESCE(MAX({key}), 0) FROM {table}").fetchone()[0]
            row = after.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
            if highest and (not row or row[0] < highest):
                problems.append(f"{table} sequence is below its highest key, so a key could be reused")

        words = sorted({w for (name,) in after.execute("SELECT name FROM models") for w in name.split() if len(w) >= 3})
        random.Random(7).shuffle(words)
        for word in words[:SEARCH_TERMS]:
            pattern = f"%{word}%"
            plain = {r[0] for r in after.execute("""
                SELECT m.id FROM models m WHERE m.name LIKE ?1 OR m.designer_name LIKE ?1
                OR m.id IN (SELECT model_id FROM model_tags WHERE tag LIKE ?1)
            """, (pattern,))}
            indexed = {r[0] for r in after.execute("""
                SELECT rowid FROM catalog_search WHERE name LIKE ?1
                UNION SELECT rowid FROM catalog_search WHERE designer_name LIKE ?1
                UNION SELECT model_id FROM model_tags WHERE tag LIKE ?1
                    AND model_id IN (SELECT rowid FROM catalog_search WHERE tags LIKE ?1)
            """, (pattern,))}
            if plain != indexed:
                problems.append(f"search for {word!r} differs from the index")
                break

        return {"rows_compared": compared, "problems": problems, "passed": not problems}
    finally:
        before.close()
        after.close()


def smoke(stage_dir: Path) -> dict[str, Any]:
    """Runs the real app on the migrated copy, with workers off and outside network blocked."""
    app_root = stage_dir / "old_app"
    (stage_dir / ".desync_dev_data").touch()
    (stage_dir / "baseline").mkdir(exist_ok=True)
    conn = open_read_only(app_root / "library.db")

    def pick(sql: str) -> Any:
        return (conn.execute(sql).fetchone() or [None])[0]

    mmf = pick(f"SELECT id FROM models WHERE source = 'mmf' AND {VISIBLE} LIMIT 1")
    external = pick("SELECT id FROM models WHERE source = 'external' LIMIT 1")
    stash = pick("SELECT id FROM stashes LIMIT 1")
    word = pick(f"SELECT name FROM models WHERE {VISIBLE} LIMIT 1")
    total = pick(f"SELECT COUNT(*) FROM models WHERE {VISIBLE}")
    conn.close()

    paths = ["/api/metrics", "/api/models?limit=24", "/api/releases?limit=24", "/api/creators", "/api/stashes",
             "/api/settings/libraries", "/api/reconcile/duplicates", "/api/downloads/status"]
    if word:
        paths.append(f"/api/models?limit=24&search={quote(word.split()[0])}")
    for model_id in (mmf, external):
        if model_id:
            paths += [f"/api/models/{model_id}", f"/api/models/{model_id}/files"]
    if stash:
        paths.append(f"/api/stashes/{stash}")

    env = {**os.environ, "DESYNC_ALLOWED_ROOTS": str(stage_dir)}
    with open(stage_dir / "smoke.log", "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "scripts" / "baseline" / "old_app_server.py"),
             "--app-root", str(app_root), "--code", "new"],
            stdout=subprocess.PIPE, stderr=log, text=True, env=env,
        )
    problems = []
    results = {}
    try:
        port = None
        assert process.stdout
        for line in process.stdout:
            if line.startswith("READY "):
                port = int(line.split()[1])
                break
        if port is None:
            return {"problems": [f"the app did not start. See {stage_dir / 'smoke.log'}"], "passed": False}

        for path in paths:
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            client.request("GET", path, headers={"Host": "localhost"})
            response = client.getresponse()
            body = response.read()
            client.close()
            results[path] = response.status
            if response.status != 200:
                problems.append(f"{path} returned {response.status}")
            elif path == "/api/metrics" and json.loads(body).get("total_models") != total:
                problems.append(f"metrics show {json.loads(body).get('total_models')} models, the database has {total}")
    finally:
        process.terminate()
        process.wait(timeout=15)
    return {"requests": results, "problems": problems, "passed": not problems}


def run_stage(name: str, source: Path, workdir: Path, size: int | None) -> dict[str, Any]:
    from desync_archival import migrations
    from desync_archival.services import secret_store

    stage_dir = workdir / name
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    (stage_dir / "old_app").mkdir(parents=True)
    before = stage_dir / "before.db"
    live = stage_dir / "old_app" / "library.db"

    copy_database(source, before)
    report: dict[str, Any] = {"stage": name, "source": str(source), "finished_at": datetime.now(UTC).isoformat()}
    if size:
        report["sample_model_ids"] = reduce_to_sample(before, size)
    copy_database(before, live)
    os.environ["DESYNC_SECRETS_FILE"] = str(stage_dir / "secrets.json")

    started = time.perf_counter()
    result = migrations.migrate_database(live)
    report["migration"] = {
        "from": result["from"], "to": result["to"], "backup": str(result["backup"]),
        "seconds": round(time.perf_counter() - started, 2),
    }
    report["paths_rewritten"] = rewrite_paths(live, source.parent)
    conn = sqlite3.connect(live)
    try:
        report["secrets_exported"] = secret_store.export_from_database(conn)
        held = secret_store.load()
        report["secrets_match"] = all(held.get(k) == v for k, v in secret_store.stored_in_database(conn).items())
    finally:
        conn.close()
    report["verify"] = verify(before, live, old_root=source.parent)
    report["paths_isolated"] = isolate_paths(live, stage_dir)
    report["smoke"] = smoke(stage_dir)
    report["passed"] = report["verify"]["passed"] and report["smoke"]["passed"] and report["secrets_match"]
    (stage_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def app_is_running(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def only_in_rolled_back(current: Path, restored: Path) -> dict[str, list]:
    """Rows the restore dropped. Files behind them may still be in the vault, unknown to the old database."""
    newer, older = open_read_only(current), open_read_only(restored)
    try:
        models = {r[0]: r[1] for r in newer.execute("SELECT id, name FROM models")}
        kept = {r[0] for r in older.execute("SELECT id FROM models")}
        files = {r[0]: r[1:] for r in newer.execute("SELECT file_id, model_id, cleaned_filename, vault_path FROM downloaded_files")}
        kept_files = {r[0] for r in older.execute("SELECT file_id FROM downloaded_files")}
        return {
            "models": [{"id": k, "name": v} for k, v in models.items() if k not in kept],
            "downloaded_files": [
                {"file_id": k, "model_id": v[0], "filename": v[1], "vault_path": v[2]}
                for k, v in files.items() if k not in kept_files
            ],
        }
    finally:
        newer.close()
        older.close()


def rollback(data_dir: Path, backup_file: Path, port: int, confirm: bool) -> dict[str, Any]:
    from desync_archival import backup, settings

    live = data_dir / "library.db"
    settings.check_path(live)
    counts = backup.verify(backup_file)
    plan = {
        "data_dir": str(data_dir), "backup": str(backup_file), "backup_rows": sum(counts.values()),
        "app_running": app_is_running(port), "confirmed": confirm,
    }
    if plan["app_running"]:
        return {**plan, "passed": False, "problems": [f"Stop the app on port {port} first."]}
    if not confirm:
        return {**plan, "passed": True, "problems": [], "note": "Dry run. Add --confirm to roll back."}

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    aside = data_dir / f"rolled_back_{stamp}"
    aside.mkdir()
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{live}{suffix}")
        if path.exists():
            shutil.move(str(path), aside / path.name)

    try:
        backup.restore(backup_file, live)
    except BaseException:
        for path in aside.iterdir():
            shutil.move(str(path), data_dir / path.name)
        aside.rmdir()
        raise

    dropped = only_in_rolled_back(aside / "library.db", live)
    report = {
        **plan, "rolled_back_copy": str(aside), "dropped": dropped, "passed": True, "problems": [],
        "warning": "Do not run delete-orphans until a reconcile has reviewed the files listed in dropped.",
    }
    (aside / "rollback_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def print_result(result: dict[str, Any]) -> int:
    # Names of secrets are fine to show. Values never enter a report.
    print(json.dumps({k: v for k, v in result.items() if k != "sample_model_ids"}, indent=2))
    return 0 if result.get("passed") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("preflight")
    check.add_argument("--source", type=Path, required=True)

    for name in ("sample", "full"):
        stage = commands.add_parser(name)
        stage.add_argument("--source", type=Path, required=True)
        stage.add_argument("--workdir", type=Path, required=True)
        if name == "sample":
            stage.add_argument("--models", type=int, default=SAMPLE_SIZE)

    compare = commands.add_parser("verify")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--old-root", type=Path)
    compare.add_argument("--rows-only", action="store_true")

    rewrite = commands.add_parser("rewrite-paths")
    rewrite.add_argument("database", type=Path)
    rewrite.add_argument("--old-root", type=Path, required=True)

    undo = commands.add_parser("rollback")
    undo.add_argument("--data-dir", type=Path, required=True)
    undo.add_argument("--backup", type=Path, required=True)
    undo.add_argument("--port", type=int, default=8080)
    undo.add_argument("--confirm", action="store_true")

    args = parser.parse_args()
    if args.command == "preflight":
        return print_result(preflight(args.source))
    if args.command == "verify":
        return print_result(verify(args.before, args.after, args.rows_only, args.old_root))
    if args.command == "rewrite-paths":
        return print_result(rewrite_stored_paths(args.database.resolve(), args.old_root))
    if args.command == "rollback":
        return print_result(rollback(args.data_dir.resolve(), args.backup.resolve(), args.port, args.confirm))

    workdir = args.workdir.resolve()
    if workdir in (args.source.resolve().parent, *args.source.resolve().parents):
        print("--workdir must not contain the source database", file=sys.stderr)
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["DESYNC_BACKUP_DIR"] = str(workdir / "backups")
    os.environ["DESYNC_DATA_DIR"] = str(workdir / "data")
    size = args.models if args.command == "sample" else None
    return print_result(run_stage(args.command, args.source.resolve(), workdir, size))


if __name__ == "__main__":
    sys.exit(main())

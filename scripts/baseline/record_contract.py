#!/usr/bin/env python3
"""Records API contract snapshots and a performance baseline from the throwaway old app.

Each run starts from a fresh copy of the sanitized database. Two runs are recorded and compared,
so any field that changes between runs is reported instead of being stored as a false contract.
"""

import argparse
import hashlib
import http.client
import json
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract_tools import find_leaks, normalize

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = REPO_ROOT / "tests" / "contract" / "snapshots"
PERFORMANCE_FILE = REPO_ROOT / "tests" / "contract" / "performance_baseline.json"
SECRET_KEYS = (
    "PHPSESSID", "cf_clearance", "cookie_cf_clearance", "cookie_phpsessid", "cookies_json",
    "mmf_access_token", "mmf_refresh_token", "mmf_client_secret",
)
PERFORMANCE_REQUESTS = {
    "catalog_first_page": "/api/models?limit=24",
    "catalog_search": "/api/models?limit=24&search=dragon",
    "catalog_search_no_match": "/api/models?limit=24&search=zzzqqq",
    "catalog_search_short": "/api/models?limit=24&search=dr",
    "catalog_search_common": "/api/models?limit=24&search=the",
    "catalog_deep_page": "/api/models?limit=24&offset=4800",
    "metrics": "/api/metrics",
    "creators": "/api/creators",
    "releases": "/api/releases?limit=24",
}
PERFORMANCE_REPEATS = 15
CLEAN_DB = "library.clean.db"


@dataclass
class Case:
    """One request in the API contract. wait marks cases that start a background task."""
    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    wait: bool = False


def build_cases(db_path: Path, dest: Path) -> list[Case]:
    """Builds the contract from real IDs in the throwaway database, so no snapshot hard-codes a row."""
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    model = conn.execute("SELECT id FROM models WHERE source = 'mmf' ORDER BY id LIMIT 1").fetchone()[0]
    stored = conn.execute("SELECT model_id FROM downloaded_files ORDER BY model_id LIMIT 1").fetchone()[0]
    external = conn.execute("SELECT id FROM models WHERE source = 'external' ORDER BY id LIMIT 1").fetchone()[0]
    file_row = conn.execute("SELECT file_id FROM downloaded_files ORDER BY file_id LIMIT 1").fetchone()[0]
    archive = conn.execute("SELECT file_id FROM file_archive_submodels ORDER BY file_id LIMIT 1").fetchone()[0]
    stash = conn.execute("SELECT id FROM stashes ORDER BY id LIMIT 1").fetchone()[0]
    item = conn.execute("SELECT id FROM stash_items ORDER BY id LIMIT 1").fetchone()[0]
    library = conn.execute("SELECT id FROM external_libraries ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    vault = dest / "nas" / "vault"
    library_path = str(dest / "nas" / "contract_library")
    Path(library_path).mkdir(parents=True, exist_ok=True)

    return [
        # Reads
        Case("metrics", "GET", "/api/metrics"),
        Case("models", "GET", "/api/models?limit=5"),
        Case("models_search", "GET", "/api/models?limit=5&search=dragon"),
        Case("models_filters", "GET", "/api/models?limit=5&bonus=1&sort=name"),
        Case("model_detail", "GET", f"/api/models/{stored}"),
        Case("model_detail_missing", "GET", "/api/models/1"),
        Case("model_detail_alias", "GET", f"/api/models/detail?id={stored}"),
        Case("model_item_alias", "GET", f"/api/models/item?id={model}"),
        Case("model_files", "GET", f"/api/models/{stored}/files"),
        Case("model_files_alias", "GET", f"/api/models/files?model_id={stored}"),
        Case("creators", "GET", "/api/creators"),
        Case("releases", "GET", "/api/releases?limit=5"),
        Case("audit", "GET", "/api/audit"),
        Case("archive_submodels", "GET", f"/api/archives/{archive}/submodels"),
        Case("archive_submodels_alias", "GET", f"/api/models/archives?file_id={archive}"),
        Case("archive_raw", "GET", f"/api/archives/{archive}/raw-tree"),
        Case("archive_raw_alias", "GET", f"/api/models/archives/raw?file_id={archive}"),
        Case("downloads_status", "GET", "/api/downloads/status"),
        Case("downloads_queue_get", "GET", "/api/downloads/queue"),
        Case("downloads_history", "GET", "/api/downloads/history"),
        Case("settings", "GET", "/api/settings"),
        Case("settings_directories", "GET", "/api/settings/directories"),
        Case("settings_logging", "GET", "/api/settings/logging"),
        Case("settings_libraries", "GET", "/api/settings/libraries"),
        Case("settings_libraries_slash", "GET", "/api/settings/libraries/"),
        Case("system_browse", "GET", f"/api/system/browse?path={dest / 'nas'}"),
        Case("storage_status", "GET", "/api/storage/status"),
        Case("auth_status", "GET", "/api/auth/status"),
        Case("auth_login", "GET", "/api/auth/login"),
        Case("auth_login_url", "GET", "/api/auth/login-url"),
        Case("auth_callback", "GET", "/api/auth/callback"),
        Case("tasks", "GET", "/api/tasks"),
        Case("tasks_status", "GET", "/api/tasks/status"),
        Case("scrapes_list", "GET", "/api/scrapes/list"),
        Case("scrapes_download_missing", "GET", "/api/scrapes/download?path=scrapes/none/model_ids.txt"),
        Case("reconcile_status", "GET", "/api/reconcile/status"),
        Case("reconcile_staging", "GET", "/api/reconcile/staging"),
        Case("reconcile_staging_alias", "GET", "/api/reconcile/staging-garbage"),
        Case("reconcile_drift", "GET", "/api/reconcile/drift"),
        Case("reconcile_path_drift", "GET", "/api/reconcile/path-drift"),
        Case("reconcile_drift_scan", "GET", "/api/reconcile/drift-scan"),
        Case("stashes", "GET", "/api/stashes"),
        Case("stashes_slash", "GET", "/api/stashes/"),
        Case("stash_active", "GET", "/api/stashes/active"),
        Case("stash_active_slash", "GET", "/api/stashes/active/"),
        Case("stash_detail", "GET", f"/api/stashes/{stash}"),
        Case("not_found", "GET", "/api/does-not-exist"),
        # Writes against the throwaway database
        Case("settings_save", "POST", "/api/settings", {"worker_concurrency": "2"}),
        Case("settings_logging_save", "POST", "/api/settings/logging", {"level": "INFO", "layers": []}),
        Case("storage_config", "POST", "/api/storage/config", {
            "download_root_path": str(dest / "nas" / "vault"),
            "download_work_path": str(dest / "staging"),
        }),
        Case("library_add", "POST", "/api/settings/libraries", {
            "name": "Contract Library", "path": library_path, "default_creator": "Contract",
        }),
        Case("library_add_slash", "POST", "/api/settings/libraries/", {}),
        Case("library_scan", "POST", f"/api/settings/libraries/{library}/scan", {}, wait=True),
        Case("library_scan_slash", "POST", f"/api/settings/libraries/{library}/scan/", {}, wait=True),
        Case("auth_config", "POST", "/api/auth/config", {
            "client_id": "contract-client", "client_secret": "contract-secret-value",
        }),
        Case("auth_token_callback", "POST", "/api/auth/token_callback", {
            "access_token": "contract-token-value", "expires_in": 60,
        }),
        Case("auth_disconnect", "POST", "/api/auth/disconnect", {}),
        Case("downloads_queue_add", "POST", "/api/downloads/queue", {"model_ids": [model]}),
        Case("downloads_dequeue", "POST", "/api/downloads/dequeue", {"model_ids": [model]}),
        Case("downloads_queue_delete", "DELETE", "/api/downloads/queue", {"model_ids": [model]}),
        Case("downloads_control_pause", "POST", "/api/downloads/control", {"action": "pause"}),
        Case("downloads_control_unknown", "POST", "/api/downloads/control", {"action": "nope"}),
        Case("stash_create", "POST", "/api/stashes", {"name": "Contract", "make_active": False}),
        Case("stash_create_slash", "POST", "/api/stashes/", {"name": ""}),
        Case("stash_set_active", "POST", f"/api/stashes/{stash}/active", {}),
        Case("stash_item_add", "POST", f"/api/stashes/{stash}/items", {
            "item_type": "file", "model_id": stored, "file_id": file_row,
        }),
        Case("stash_item_refused", "POST", f"/api/stashes/{stash}/items", {
            "item_type": "model", "model_id": model,
        }),
        Case("stash_bulk", "POST", f"/api/stashes/{stash}/bulk-models", {"model_ids": [stored]}),
        Case("stash_export", "POST", f"/api/stashes/{stash}/export", {"sync": True, "filter": "all"}),
        Case("render_view", "POST", f"/api/models/{external}/render-view", {"view": "bad-view"}),
        Case("render_stl", "POST", "/api/render/stl", {"source_type": "file", "id": file_row, "view": "bad-view"}),
        Case("render_stl_slash", "GET", "/api/render/stl/?source_type=file&view=bad-view"),
        Case("archive_peek", "POST", "/api/reconcile/archive-peek", {"model_ids": [stored]}, wait=True),
        Case("reconcile_scan", "POST", "/api/reconcile/scan", {"scan_mode": "fast"}, wait=True),
        Case("reconcile_cancel", "POST", "/api/reconcile/cancel", {}),
        Case("reconcile_catalog_previews", "POST", "/api/reconcile/catalog-previews", {"model_ids": [stored]}, wait=True),
        Case("reconcile_path_drift_post", "POST", "/api/reconcile/drift", {}),
        Case("reconcile_drift_dryrun", "POST", "/api/reconcile/path-drift", {}),
        Case("reconcile_realign", "POST", "/api/reconcile/drift/realign", {"model_ids": []}),
        Case("reconcile_path_realign", "POST", "/api/reconcile/path-realign", {"model_ids": []}),
        Case("reconcile_ingest_untracked_empty", "POST", "/api/reconcile/ingest-untracked", {"items": []}),
        Case("reconcile_ingest_untracked", "POST", "/api/reconcile/ingest-untracked", {
            "items": [{"model_id": stored, "file_path": str(vault / "contract_untracked" / "extra.stl")}],
        }, wait=True),
        # Network and node are blocked in the harness, so these record the failure paths.
        Case("scraper_start", "POST", "/api/scraper/start", {}, wait=True),
        Case("tasks_scrape", "POST", "/api/tasks/scrape", {}, wait=True),
        Case("ingest_invalid", "POST", "/api/ingest/start", {}),
        Case("tasks_ingest_invalid", "POST", "/api/tasks/ingest", {"raw_ids": "none"}),
        Case("ingest_ids", "POST", "/api/ingest/start", {"raw_ids": str(model)}, wait=True),
        Case("tasks_cancel", "POST", "/api/tasks/cancel", {}),
        Case("tasks_kill", "POST", "/api/tasks/kill", {}),
        # Destructive routes, last
        Case("stash_item_delete", "DELETE", f"/api/stashes/items/{item}"),
        Case("stash_delete", "DELETE", f"/api/stashes/{stash}"),
        Case("reconcile_delete_orphans_empty", "POST", "/api/reconcile/delete-orphans", {"paths": []}),
        Case("reconcile_delete_orphans_dry_run", "POST", "/api/reconcile/delete-orphans", {
            "paths": [str(vault / "contract_orphan_one")],
        }),
        Case("reconcile_delete_orphans", "POST", "/api/reconcile/delete-orphans", {
            "paths": [str(vault / "contract_orphan_one")], "confirm": True,
        }),
        Case("reconcile_orphan_audit", "POST", "/api/reconcile/orphan-audit", {
            "paths": [str(vault / "contract_orphan_two")], "confirm": True,
        }),
        Case("reconcile_staging_purge", "POST", "/api/reconcile/staging/purge", {"paths": [], "confirm": True}),
        Case("reconcile_staging_purge_dry_run", "POST", "/api/reconcile/staging-purge", {"paths": []}),
        Case("reconcile_staging_purge_alias", "POST", "/api/reconcile/staging-purge", {
            "paths": [str(dest / "staging" / "contract_garbage.part")], "confirm": True,
        }),
        Case("reconcile_staging_sweep", "POST", "/api/reconcile/staging-sweep", {"paths": [], "confirm": True}),
        Case("reconcile_reset_empty", "POST", "/api/reconcile/reset", {}),
        Case("reconcile_reset_dry_run", "POST", "/api/reconcile/reset", {"model_ids": [stored]}),
        Case("reconcile_reset", "POST", "/api/reconcile/reset", {"model_ids": [stored], "confirm": True}),
        Case("downloads_control_purge", "POST", "/api/downloads/control", {"action": "purge_incomplete"}),
        Case("library_delete", "DELETE", f"/api/settings/libraries/{library}"),
        Case("logs_purge", "POST", "/api/settings/logs/purge", {}),
        Case("stashes_purge_dry_run", "POST", "/api/stashes/purge", {}),
        Case("stashes_purge", "POST", "/api/stashes/purge", {"confirm": True}),
        Case("settings_stashes_purge", "POST", "/api/settings/stashes/purge", {"confirm": True}),
        Case("tasks_clear_dry_run", "POST", "/api/tasks/clear", {}),
        Case("tasks_clear", "POST", "/api/tasks/clear", {"confirm": True}),
    ]


def request(port: int, case: Case, timeout: float = 180) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    body = json.dumps(case.body) if case.body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request(case.method, case.path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result: dict[str, Any] = {
            "status": response.status,
            "content_type": response.getheader("Content-Type"),
            "location": response.getheader("Location"),
        }
    finally:
        connection.close()

    try:
        result["json"] = json.loads(payload)
    except ValueError:
        try:
            result["text"] = payload.decode("utf-8")
        except UnicodeDecodeError:
            result["binary"] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def wait_for_idle(port: int, limit: float = 60) -> None:
    """Polls until background tasks finish, so the next case sees a settled database."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        status = request(port, Case("poll", "GET", "/api/tasks/status")).get("json")
        reconcile = request(port, Case("poll", "GET", "/api/reconcile/status")).get("json")
        active = isinstance(status, dict) and status.get("active_task")
        running = isinstance(reconcile, dict) and reconcile.get("status") == "running"
        if not active and not running:
            return
        time.sleep(0.25)
    print("warning: background work still active after the wait limit", file=sys.stderr)


class Harness:
    """Starts one server against a throwaway copy and always stops it."""
    def __init__(self, dest: Path, code: str = "old"):
        self.dest = dest
        self.code = code
        self.app = dest / "old_app"
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.startup_ms = 0.0

    def prepare_storage(self) -> None:
        """Resets dev storage, with placeholders so routes reach their success paths."""
        vault = self.dest / "nas" / "vault"
        for target in (vault, self.dest / "stash", self.dest / "staging"):
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)

        conn = sqlite3.connect(f"{(self.dest / CLEAN_DB).as_uri()}?mode=ro", uri=True)
        stored = conn.execute("SELECT model_id FROM downloaded_files ORDER BY model_id LIMIT 1").fetchone()[0]
        rows = conn.execute("SELECT vault_path FROM downloaded_files WHERE model_id = ?", (stored,)).fetchall()
        conn.close()
        for (vault_path,) in rows:
            placeholder = Path(vault_path)
            if not placeholder.is_relative_to(vault):
                raise RuntimeError("A dev vault path points outside the dev vault")
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            placeholder.write_bytes(b"contract placeholder")

        for name in ("contract_orphan_one", "contract_orphan_two", "contract_untracked"):
            (vault / name).mkdir()
        (vault / "contract_untracked" / "extra.stl").write_bytes(b"contract placeholder")
        (self.dest / "staging" / "contract_garbage.part").write_bytes(b"contract placeholder")

    def __enter__(self) -> Self:
        self.prepare_storage()
        for name in ("logs", "scrapes", "staging"):
            target = self.app / name
            if target.is_dir():
                shutil.rmtree(target)
        # Stale WAL files beside a fresh copy would corrupt it, so they go first.
        for suffix in ("-journal", "-wal", "-shm"):
            (self.app / f"library.db{suffix}").unlink(missing_ok=True)
        shutil.copy2(self.dest / CLEAN_DB, self.app / "library.db")

        log = open(self.dest / "baseline" / "old_app.stderr.log", "w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "scripts" / "baseline" / "old_app_server.py"),
             "--app-root", str(self.app), "--code", self.code],
            stdout=subprocess.PIPE, stderr=log, text=True,
        )
        log.close()
        assert self.process.stdout
        for line in self.process.stdout:
            if line.startswith("READY "):
                _, port, startup = line.split()
                self.port, self.startup_ms = int(port), float(startup)
                return self
        raise RuntimeError("The harness exited before it was ready")

    def __exit__(self, *_: object) -> None:
        assert self.process
        self.process.terminate()
        self.process.wait(timeout=15)


@contextmanager
def preserved_dev_storage(dest: Path) -> Iterator[None]:
    """Sets real dev files aside, because each harness run resets these folders."""
    kept = dest / "baseline" / "kept_storage"
    if kept.exists():
        raise RuntimeError(f"{kept} is left from an interrupted run. Restore it before continuing.")
    kept.mkdir()
    folders = {"vault": dest / "nas" / "vault", "stash": dest / "stash", "staging": dest / "staging"}
    for name, folder in folders.items():
        if folder.exists():
            folder.rename(kept / name)
    try:
        yield
    finally:
        for name, folder in folders.items():
            if (kept / name).exists():
                shutil.rmtree(folder, ignore_errors=True)
                (kept / name).rename(folder)
        kept.rmdir()


def record_run(dest: Path, code: str = "old") -> tuple[dict[str, Any], float, list[Case]]:
    cases = build_cases(dest / CLEAN_DB, dest)
    replacements = [(str(dest), "<DEV>")]
    snapshots: dict[str, Any] = {}
    with Harness(dest, code) as harness:
        for case in cases:
            print(f"  {case.method} {case.path}", flush=True)
            result = request(harness.port, case)
            if case.wait:
                wait_for_idle(harness.port)
            snapshots[case.name] = normalize(
                {"request": {"method": case.method, "path": case.path, "body": case.body}, "response": result},
                replacements,
            )
        wait_for_idle(harness.port)
        startup = harness.startup_ms
    return snapshots, startup, cases


def record_performance(dest: Path, code: str = "old") -> dict[str, Any]:
    """Times the catalog reads that the migration promised not to slow down.

    Raises:
        RuntimeError: The operation failed. The message says why.
    """
    results: dict[str, Any] = {}
    startups = []
    for _ in range(5):
        with Harness(dest, code) as harness:
            startups.append(harness.startup_ms)
    results["startup_ms"] = {"median": round(statistics.median(startups), 1), "max": round(max(startups), 1)}

    with Harness(dest, code) as harness:
        for name, path in PERFORMANCE_REQUESTS.items():
            times = []
            for _ in range(PERFORMANCE_REPEATS):
                started = time.perf_counter()
                result = request(harness.port, Case(name, "GET", path))
                times.append((time.perf_counter() - started) * 1000)
                if result["status"] != 200:
                    raise RuntimeError(f"{path} returned {result['status']}")
            results[name] = {
                "path": path,
                "median_ms": round(statistics.median(times), 2),
                "max_ms": round(max(times), 2),
                "repeats": PERFORMANCE_REPEATS,
            }
    return results


def route_patterns(app: Path) -> tuple[set[str], list[re.Pattern[str]]]:
    source = (app / "ui" / "routes" / "__init__.py").read_text() + (app / "ui" / "routes" / "stashes.py").read_text()
    literals = set(re.findall(r'"(/api/[^"]*)"', source))
    patterns = [re.compile(p) for p in re.findall(r'r"(\^/api/[^"]*)"', source)]
    return literals, patterns


def uncovered_routes(app: Path, cases: list[Case]) -> list[str]:
    """Finds old routes no contract case exercises, so none can dodge the contract."""
    paths = {case.path.split("?")[0] for case in cases}
    literals, patterns = route_patterns(app)
    missing = sorted(literal for literal in literals if literal not in paths)
    missing += sorted(p.pattern for p in patterns if not any(p.match(path) for path in paths))
    return missing


def differing_fields(first: Any, second: Any, location: str = "$") -> list[str]:
    """Reports fields that changed between two runs, which would otherwise be stored as false contract."""
    if type(first) is not type(second):
        return [location]
    if isinstance(first, dict):
        fields: list[str] = []
        for key in first.keys() | second.keys():
            if key not in first or key not in second:
                fields.append(f"{location}.{key}")
            else:
                fields.extend(differing_fields(first[key], second[key], f"{location}.{key}"))
        return fields
    if isinstance(first, list):
        if len(first) != len(second):
            return [location]
        fields = []
        for index, (a, b) in enumerate(zip(first, second)):
            fields.extend(differing_fields(a, b, f"{location}[{index}]"))
        return fields
    return [] if first == second else [location]


def production_secret_values(source_db: Path) -> list[str]:
    """Reads real secret values only to prove that no snapshot contains them."""
    conn = sqlite3.connect(f"{source_db.as_uri()}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(SECRET_KEYS))
        rows = conn.execute(f"SELECT value FROM app_settings WHERE key IN ({marks})", SECRET_KEYS)
        return [row[0] for row in rows if row[0] and len(row[0]) >= 8]
    finally:
        conn.close()


def compare_new_app(dest: Path) -> int:
    """Runs the migrated app through every case and compares it to the stored snapshots."""
    current, _, _ = record_run(dest, "new")
    differences = 0
    for name, snapshot in current.items():
        stored_path = SNAPSHOT_DIR / f"{name}.json"
        if not stored_path.exists():
            differences += 1
            print(f"different: {name}: no snapshot yet")
            continue
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        fields = differing_fields(stored, json.loads(json.dumps(snapshot)))
        if fields:
            differences += 1
            print(f"different: {name}: {fields[:8]}")
    print(f"compared {len(current)} cases, {differences} different")
    return 1 if differences else 0


def accept_changes(dest: Path, names: list[str]) -> int:
    """Rewrites only the named snapshots, for changes listed as intended."""
    current, _, _ = record_run(dest, "new")
    for name in names:
        snapshot = current[name]
        if find_leaks(snapshot["response"]):
            print(f"refused: {name} holds a secret or signed URL")
            return 1
        text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
        (SNAPSHOT_DIR / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"accepted {len(names)} snapshots")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", action="store_true", help="Compare the migrated app to the snapshots")
    parser.add_argument("--accept", nargs="+", default=[], help="Rewrite only these snapshots from the current app")
    parser.add_argument("--measure", type=Path, help="Write the migrated app's timings to this file")
    parser.add_argument("--dest", type=Path, default=Path.home() / "DESYNC_Data_dev")
    parser.add_argument("--source-db", type=Path, default=REPO_ROOT.parent / "scrapepage_deprecated" / "library.db")
    args = parser.parse_args()
    dest = args.dest.resolve()
    with preserved_dev_storage(dest):
        if args.measure:
            args.measure.write_text(json.dumps(record_performance(dest, "new"), indent=2) + "\n", encoding="utf-8")
            return 0
        if args.accept:
            return accept_changes(dest, args.accept)
        if args.compare:
            return compare_new_app(dest)
        return record_baseline(dest, args.source_db)


def record_baseline(dest: Path, source_db: Path) -> int:
    """Records two runs and refuses to store fields that differ between them."""
    print("Run 1")
    first, _, cases = record_run(dest)
    print("Run 2")
    second, _, _ = record_run(dest)

    unstable = {
        name: fields
        for name in first
        if (fields := differing_fields(first[name], second[name]))
    }
    missing = uncovered_routes(dest / "old_app", cases)
    leaks = {name: found for name, snapshot in first.items() if (found := find_leaks(snapshot["response"]))}

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in SNAPSHOT_DIR.glob("*.json"):
        stale.unlink()
    text_by_name = {}
    for name, snapshot in first.items():
        text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
        text_by_name[name] = text
        (SNAPSHOT_DIR / f"{name}.json").write_text(text, encoding="utf-8")

    secrets = production_secret_values(source_db)
    real_leaks = sum(1 for text in text_by_name.values() for secret in secrets if secret in text)

    print("Recording performance baseline")
    performance = record_performance(dest)
    PERFORMANCE_FILE.write_text(json.dumps(performance, indent=2) + "\n", encoding="utf-8")

    statuses: dict[int, int] = {}
    for snapshot in first.values():
        status = snapshot["response"]["status"]
        statuses[status] = statuses.get(status, 0) + 1

    print(f"cases: {len(first)}")
    print(f"statuses: {dict(sorted(statuses.items()))}")
    print(f"uncovered routes: {missing}")
    print(f"snapshot leaks by pattern: {leaks}")
    print(f"snapshots holding a real production secret: {real_leaks}")
    print(f"unstable cases: {json.dumps(unstable, indent=1)[:4000]}")
    return 1 if unstable or missing or leaks or real_leaks else 0


if __name__ == "__main__":
    sys.exit(main())

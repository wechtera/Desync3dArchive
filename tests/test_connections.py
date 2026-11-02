"""E48: database connections must give their file handles back as soon as they're done with."""

import ast
import gc
import os
import sqlite3
from pathlib import Path

import pytest

from desync_archival.services.archive_peeker import ArchivePeeker
from desync_archival.services.db import (
    ScopedConnection,
    get_db_connection,
    scoped_connection,
)
from desync_archival.services.downloader import DownloadManager
from desync_archival.services.external_indexer import ExternalLibraryIndexer
from desync_archival.services.image_worker import ImageWorker
from desync_archival.services.reconciler import VaultReconciler
from desync_archival.services.stash_service import StashService
from desync_archival.services.task_manager import TaskManager

SRC = Path(__file__).resolve().parents[1] / "src" / "desync_archival"

# The one factory, so timeouts and read-only handling can't drift apart.
DIRECT_CONNECT_ALLOWED = {"db/connection.py"}
OPENERS = {"connect", "get_db_connection", "scoped_connection", "_get_db", "_get_connection"}


def open_files() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE t (x INTEGER);")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def no_gc():
    # The bug only shows with the collector out of the way: it was the only thing closing these.
    gc.collect()
    gc.disable()
    yield
    gc.enable()


def test_scoped_connections_leave_no_open_files(db_path, no_gc):
    before = open_files()
    for _ in range(50):
        with scoped_connection(db_path) as conn:
            conn.execute("SELECT COUNT(*) FROM t;").fetchone()
    assert open_files() <= before + 2


def test_the_plain_with_block_this_replaces_really_did_hold_files(db_path, no_gc):
    def read():
        with sqlite3.connect(db_path) as conn:
            conn.execute("SELECT COUNT(*) FROM t;").fetchone()

    before = open_files()
    for _ in range(20):
        read()
    held = open_files() - before
    gc.collect()
    assert held >= 20


def test_a_nested_with_commits_and_leaves_the_connection_open(db_path):
    with scoped_connection(db_path) as conn:
        with conn:
            conn.execute("INSERT INTO t VALUES (1);")
        assert conn.execute("SELECT COUNT(*) FROM t;").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1;")


def test_a_failed_block_rolls_back_and_still_closes(db_path):
    with pytest.raises(ValueError), scoped_connection(db_path) as conn:
        conn.execute("INSERT INTO t VALUES (1);")
        raise ValueError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1;")
    with scoped_connection(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM t;").fetchone()[0] == 0


def test_the_block_commits_on_success(db_path):
    with scoped_connection(db_path) as conn:
        conn.execute("INSERT INTO t VALUES (1);")
    with scoped_connection(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM t;").fetchone()[0] == 1


def test_settings_match_the_shared_factory(db_path):
    with scoped_connection(db_path) as conn:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout;").fetchone()[0] == 45000
    with scoped_connection(db_path, foreign_keys=False) as conn:
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 0


def test_get_db_connection_stays_open_for_its_owner(db_path):
    conn = get_db_connection(db_path)
    with conn:
        conn.execute("INSERT INTO t VALUES (1);")
    assert conn.execute("SELECT COUNT(*) FROM t;").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    "cls, method",
    [
        (ArchivePeeker, "_get_db"),
        (DownloadManager, "_get_db"),
        (ExternalLibraryIndexer, "_get_db"),
        (ImageWorker, "_get_db"),
        (StashService, "_get_connection"),
        (TaskManager, "_get_db"),
        (VaultReconciler, "_get_db"),
    ],
)
def test_every_service_connection_is_scoped(db_path, cls, method):
    # object.__new__ because this checks one method, and constructors here do unrelated setup.
    service = object.__new__(cls)
    service.db_path = db_path
    conn = getattr(service, method)()
    assert isinstance(conn, ScopedConnection)
    conn.close()


def _source_files():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node):
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _with_items(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                yield node, item


def test_nothing_opens_with_sqlite3_connect_in_a_with_block():
    found = []
    for rel, tree in _source_files():
        for node, item in _with_items(tree):
            expr = item.context_expr
            if isinstance(expr, ast.Call) and ast.unparse(expr.func) == "sqlite3.connect":
                found.append(f"{rel}:{node.lineno}")
    assert not found


def test_direct_connects_stay_on_the_allowed_list():
    found = set()
    for rel, tree in _source_files():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "sqlite3.connect":
                found.add(rel)
    assert found == DIRECT_CONNECT_ALLOWED, found ^ DIRECT_CONNECT_ALLOWED


def test_no_connection_is_used_after_its_block_closed_it():
    found = []
    for rel, tree in _source_files():
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            blocks = []
            for node, item in _with_items(func):
                expr = item.context_expr
                if isinstance(expr, ast.Call) and _call_name(expr) in OPENERS and isinstance(item.optional_vars, ast.Name):
                    blocks.append((node.lineno, node.end_lineno, item.optional_vars.id))
            names = {name for _, _, name in blocks}
            for node in ast.walk(func):
                is_use = isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names
                if is_use and not any(a <= node.lineno <= b and var == node.id for a, b, var in blocks):
                    found.append(f"{rel}:{node.lineno} {node.id}")
    assert not found

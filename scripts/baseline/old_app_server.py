#!/usr/bin/env python3
"""Runs the old or migrated server code against throwaway data, for baselines only.

With --code old, it runs the old app's unmodified code from the throwaway copy. With --code new, it
runs the migrated package with that same directory as its data directory.

It differs from the old main() in four ways: it binds to loopback, it never starts download
workers, and it refuses outside network connections and node subprocesses. It will only run from
an old_app directory inside a marked dev data directory.
"""

import argparse
import ipaddress
import os
import socket
import socketserver
import subprocess
import sys
import time
from pathlib import Path

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_RealPopen = subprocess.Popen


def is_loopback(host: object) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


class NetworkBlockedError(OSError):
    pass


def guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback(address[0]):
        raise NetworkBlockedError(f"Outside connection blocked: {address[0]}")
    return _real_connect(self, address)


def guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback(address[0]):
        raise NetworkBlockedError(f"Outside connection blocked: {address[0]}")
    return _real_connect_ex(self, address)


def guarded_getaddrinfo(host, *args, **kwargs):
    if not is_loopback(host):
        raise socket.gaierror(f"Outside lookup blocked: {host}")
    return _real_getaddrinfo(host, *args, **kwargs)


class GuardedPopen(_RealPopen):
    def __init__(self, args, *rest, **kwargs):
        program = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
        if Path(str(program)).name == "node":
            raise OSError("node is blocked in the baseline harness")
        super().__init__(args, *rest, **kwargs)


def install_guards() -> None:
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    subprocess.Popen = GuardedPopen  # type: ignore[misc]


def run_server(server, data_root: Path, started: float) -> int:
    server.task_manager = server.TaskManager(server.DB_PATH, data_root)
    server.download_manager = server.DownloadManager(server.DB_PATH)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), server.RequestHandler) as httpd:
        httpd.daemon_threads = True
        startup_ms = (time.perf_counter() - started) * 1000
        print(f"READY {httpd.server_address[1]} {startup_ms:.1f}", flush=True)
        httpd.serve_forever()
    return 0


def serve_new(app_root: Path, port: int) -> int:
    os.environ["APP_ENV"] = "development"
    os.environ["DESYNC_HOST"] = "127.0.0.1"
    os.environ["DESYNC_REQUIRE_LOGIN"] = ""
    os.environ["DESYNC_TLS_CERT"] = ""
    os.environ["DESYNC_TLS_KEY"] = ""
    os.environ["DESYNC_DATA_DIR"] = str(app_root)
    # A neutral working directory shows up any code still relying on the current directory.
    os.chdir(app_root.parent / "baseline")

    started = time.perf_counter()
    import uvicorn

    from desync_archival import migrations, settings
    from desync_archival.api import server

    conn = server.get_db()
    migrations.apply_pending(conn)
    settings.check_stored_paths(conn)
    conn.close()

    server.task_manager = server.TaskManager(server.DB_PATH, settings.data_dir())
    server.download_manager = server.DownloadManager(server.DB_PATH)

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    config = uvicorn.Config(server.create_app(manage_workers=False), log_config=None, access_log=False)
    uvicorn_server = uvicorn.Server(config)
    startup_ms = (time.perf_counter() - started) * 1000
    print(f"READY {sock.getsockname()[1]} {startup_ms:.1f}", flush=True)
    uvicorn_server.run(sockets=[sock])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--code", choices=("old", "new"), default="old")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    app_root = args.app_root.resolve()
    if app_root.name != "old_app" or not (app_root.parent / ".desync_dev_data").exists():
        print(f"Refusing to run from {app_root}: not a throwaway app", file=sys.stderr)
        return 2

    install_guards()
    if args.code == "new":
        return serve_new(app_root, args.port)

    os.environ.pop("VAULT_DB_PATH", None)
    os.chdir(app_root)
    sys.path.insert(0, str(app_root))
    sys.path.insert(0, str(app_root / "ui"))

    started = time.perf_counter()
    import server  # pyright: ignore[reportMissingImports]  # the old ui/server.py

    if not Path(server.DB_PATH).is_relative_to(app_root):
        print("Refusing to run: the database is outside the throwaway app", file=sys.stderr)
        return 2

    conn = server.get_db()
    server.init_persistent_tables(conn)
    conn.close()

    return run_server(server, server.ROOT_DIR, started)


if __name__ == "__main__":
    sys.exit(main())

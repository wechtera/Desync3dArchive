"""Runs the FastAPI app on a loopback port inside a test process."""

import socket
import threading
import time

import uvicorn

from desync_archival.api import server


class LiveServer:
    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        sock = socket.socket()
        sock.bind((self.host, 0))
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(server.create_app(manage_workers=False), log_config=None, access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("The test server did not start")
            time.sleep(0.01)
        return self.port

    def stop(self) -> None:
        if self._server and self._thread:
            self._server.should_exit = True
            self._thread.join(timeout=10)

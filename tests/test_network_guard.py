"""The conftest guard: a test can talk to this machine, never to the internet."""

import socket
import unittest

import httpx
from conftest import NetworkBlocked


class NetworkGuardTests(unittest.TestCase):
    def test_a_real_address_is_refused_before_anything_is_sent(self):
        # 192.0.2.1 is reserved for documentation, so nothing answers even if the guard failed.
        with socket.socket() as sock, self.assertRaises(NetworkBlocked):
            sock.connect(("192.0.2.1", 80))

    def test_httpx_surfaces_it_rather_than_a_connection_error(self):
        with httpx.Client() as client, self.assertRaises(NetworkBlocked):
            client.get("https://192.0.2.1/")

    def test_this_machine_is_still_reachable(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            with socket.socket() as sock:
                self.assertEqual(sock.connect_ex(server.getsockname()), 0)

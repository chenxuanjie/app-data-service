import importlib
import socket
import socketserver
import sys
import threading
from pathlib import Path

import pytest


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("wsl_port_relay")


def test_stdio_transport_preserves_binary_data_and_half_close(relay):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            payload = bytearray()
            while chunk := self.request.recv(65536):
                payload.extend(chunk)
            self.request.sendall(payload)

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo) as upstream:
        remote = threading.Thread(target=upstream.serve_forever, daemon=True)
        remote.start()
        command = [sys.executable, "-u", "-c", relay.REMOTE, str(upstream.server_address[1])]
        with relay.Relay(("127.0.0.1", 0), command) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with socket.create_connection(server.server_address, timeout=5) as connection:
                    connection.settimeout(5)
                    payload = bytes(range(256)) * 2048
                    connection.sendall(payload)
                    connection.shutdown(socket.SHUT_WR)
                    received = bytearray()
                    while chunk := connection.recv(65536):
                        received.extend(chunk)
                    assert received == payload
            finally:
                server.shutdown()
                server.close_sessions()
                worker.join(timeout=5)
        upstream.shutdown()
        remote.join(timeout=5)


def test_healthy_native_forwarding_does_not_launch_a_relay(relay, monkeypatch):
    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(relay.socket, "create_connection", lambda *a, **k: Connection())

    def unexpected(*args, **kwargs):
        raise AssertionError("Healthy native forwarding must be reused")

    monkeypatch.setattr(relay, "Relay", unexpected)
    with relay.database_transport("127.0.0.1", 55432):
        pass


def test_occupied_broken_forwarding_does_not_kill_existing_owner(relay, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("occupied")

    monkeypatch.setattr(relay.socket, "create_connection", unavailable)
    monkeypatch.setattr(relay, "Relay", unavailable)
    with pytest.raises(RuntimeError, match="No process was killed"):
        with relay.database_transport("127.0.0.1", 55432):
            pytest.fail("An unavailable port cannot start a test run")

"""Real loopback TLS verifies HTTPX read deadlines; MockTransport cannot do that."""

import asyncio
import ipaddress
import json
import logging
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app_data_service.dsm import DsmFailure, DsmProvider


@pytest.fixture
def cold_dsm(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-dsm.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    state = {"slow": "login", "delay": 9.0, "counts": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            fields = {
                k: v[0]
                for k, v in parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()
                ).items()
            }
            phase = (
                "identity_check" if fields["api"] == "SYNO.FileStation.Info" else fields["method"]
            )
            state["counts"][phase] = state["counts"].get(phase, 0) + 1
            if phase == state["slow"] and state["counts"][phase] == 1:
                time.sleep(state["delay"])
            if fields["api"] == "SYNO.API.Info":
                data = {
                    "SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7},
                    "SYNO.FileStation.Info": {
                        "path": "entry.cgi",
                        "minVersion": 1,
                        "maxVersion": 2,
                    },
                }
            elif phase == "login":
                data = {"sid": "fixture-session", "synotoken": "fixture-token"}
            elif phase == "identity_check":
                if self.headers.get("Cookie") != "id=fixture-session":
                    self.send_error(401)
                    return
                data = {"uid": 1001}
            else:
                data = {}
            payload = json.dumps({"success": True, "data": data}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass  # The bounded-timeout tests intentionally close the client.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield (
            DsmProvider(
                f"https://127.0.0.1:{server.server_port}",
                verify=ssl.create_default_context(cafile=str(certificate_path)),
            ),
            state,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("phase", ["login", "identity_check"])
def test_first_login_handles_nine_second_cold_dsm_without_resubmitting_password(cold_dsm, phase):
    provider, state = cold_dsm
    state["slow"] = phase

    async def run():
        started = time.monotonic()
        verified = await provider.login("fixture-account", "fixture-password", None)
        assert time.monotonic() - started >= 9
        assert verified.display_name == "fixture-account"
        assert state["counts"]["login"] == 1
        assert state["counts"]["identity_check"] == 1
        await provider.logout(verified.context)

    asyncio.run(run())


def test_slow_dsm_still_stops_at_a_bounded_read_deadline(cold_dsm, monkeypatch, caplog):
    provider, state = cold_dsm
    state["delay"] = 0.3
    monkeypatch.setattr(provider, "LOGIN_READ_TIMEOUT", 0.05)
    caplog.set_level(logging.WARNING, logger="app_data_service.dsm")
    with pytest.raises(DsmFailure) as failure:
        asyncio.run(provider.login("fixture-account", "fixture-password", None))
    assert (failure.value.code, failure.value.status) == ("provider_timeout", 504)
    assert state["counts"]["login"] == 1
    assert "phase=login reason=ReadTimeout" in caplog.text
    assert "fixture-password" not in caplog.text
    assert "fixture-account" not in caplog.text


def test_login_budget_does_not_lengthen_refresh_or_file_requests():
    provider = DsmProvider("https://dsm.test")

    async def run():
        async with provider.client() as normal, provider.client(for_login=True) as login:
            assert normal.timeout.read == 8
            assert login.timeout.read == 20
            assert normal.timeout.connect == login.timeout.connect == 4

    asyncio.run(run())


def test_upstream_timeout_details_are_not_logged(caplog):
    def fail(request):
        raise httpx.ReadTimeout("secret-password and secret-session", request=request)

    provider = DsmProvider("https://dsm.test", transport=httpx.MockTransport(fail))
    caplog.set_level(logging.WARNING, logger="app_data_service.dsm")
    with pytest.raises(DsmFailure):
        asyncio.run(provider.login("private-account", "private-password", None))
    assert "phase=discovery reason=ReadTimeout" in caplog.text
    assert not any(value in caplog.text for value in ["secret-", "private-", "https://"])

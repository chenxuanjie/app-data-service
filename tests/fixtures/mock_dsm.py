"""Strict TLS DSM substitute for local container validation only."""

import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from uuid import uuid4

sessions = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        fields = {
            key: value[0]
            for key, value in parse_qs(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            ).items()
        }
        if fields.get("api") == "SYNO.API.Info":
            body = {
                "success": True,
                "data": {
                    name: {"path": "entry.cgi", "minVersion": 1, "maxVersion": maximum}
                    for name, maximum in [("SYNO.API.Auth", 7), ("SYNO.FileStation.Info", 2)]
                },
            }
        elif fields.get("method") == "login":
            if (
                fields.get("account") not in {"alice", "bob"}
                or fields.get("passwd") != "fixture-password"
            ):
                body = {"success": False, "error": {"code": 400}}
            else:
                sid = uuid4().hex
                sessions[sid] = 10001 if fields["account"] == "alice" else 10002
                body = {"success": True, "data": {"sid": sid, "synotoken": "fixture-syno"}}
        else:
            sid = self.headers.get("Cookie", "").removeprefix("id=")
            if (
                sid not in sessions
                or self.headers.get("X-SYNO-TOKEN") != "fixture-syno"
                or "_sid" in fields
            ):
                body = {"success": False, "error": {"code": 119}}
            elif fields.get("method") == "logout":
                sessions.pop(sid)
                body = {"success": True}
            else:
                body = {"success": True, "data": {"uid": sessions[sid]}}
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 9443), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(sys.argv[1], sys.argv[2])
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()

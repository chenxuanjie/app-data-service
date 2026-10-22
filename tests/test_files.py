import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from test_sessions import authenticated as authenticated_fixture
from test_sessions import login

from app_data_service.api import create_app
from app_data_service.files import relative_path


@pytest.fixture(name="authenticated")
def file_authenticated(db):
    yield from authenticated_fixture.__wrapped__(db)


@pytest.fixture
def files(authenticated, db):
    _, dsm, authority, manager = authenticated

    class Download:
        status = 200
        code = None
        headers = {}
        body = b"package"
        seen = []
        manifest = {
            "schemaVersion": 1,
            "appId": "zheri",
            "channel": "stable",
            "versionName": "0.13.0",
            "buildNumber": 64,
            "artifacts": [
                {
                    "platform": "android",
                    "architecture": "arm64",
                    "path": "zheri.apk",
                    "size": 7,
                    "sha256": "a" * 64,
                }
            ],
        }

        def request(self, request):
            values = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if values.get("query") == "SYNO.FileStation.Download":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "SYNO.FileStation.Download": {
                                "path": "entry.cgi",
                                "minVersion": 1,
                                "maxVersion": 2,
                            }
                        },
                    },
                )
            if values["api"] != "SYNO.FileStation.Download":
                return dsm.request(request)
            self.seen.append(request)
            assert request.method == "POST" and not request.url.query
            assert request.headers["X-SYNO-TOKEN"] == "fixture-syno"
            assert request.headers["Cookie"].removeprefix("id=") in dsm.sessions
            assert not {"_sid", "password", "account", "SynoToken"} & values.keys()
            assert json.loads(values["mode"]) == "download"
            path = json.loads(values["path"])[0]
            assert path.startswith(("/approved/updates/", "/home/Apps/zheri/"))
            if self.code:
                return httpx.Response(200, json={"success": False, "error": {"code": self.code}})
            if path.endswith("manifest.json"):
                payload = json.dumps(self.manifest).encode()
            else:
                payload = self.body
            return httpx.Response(
                self.status,
                content=payload,
                headers={"Content-Type": "application/octet-stream", **self.headers},
            )

    download = Download()
    manager.provider.transport = httpx.MockTransport(download.request)
    with TestClient(
        create_app(
            db.runtime,
            authority,
            "https://data.test/powersync",
            sessions=manager,
            file_roots={"updates": "/approved/updates", "resources": "/home/Apps/zheri"},
        )
    ) as client:
        account = login(client)
        client.headers["Authorization"] = "Bearer " + account["access_token"]
        yield client, download, account, dsm


def test_manifest_and_files_use_current_account_without_leaking_nas_secrets(files):
    client, download, _, _ = files
    assert client.get("/v1/services").json() == {
        "file_areas": ["resources", "updates"],
        "updates": True,
    }
    manifest = client.get("/v1/updates/manifest")
    assert manifest.status_code == 200 and manifest.json() == download.manifest
    assert manifest.headers["cache-control"] == "no-store"
    response = client.get("/v1/files/updates/zheri.apk")
    assert response.content == b"package" and response.status_code == 200
    assert not {"set-cookie", "x-syno-token"} & response.headers.keys()
    assert client.put("/v1/files/updates/zheri.apk", content=b"bad").status_code == 405
    assert client.get("/v1/updates/manifest?channel=beta").status_code == 404


@pytest.mark.parametrize(
    "code,status", [(105, 403), (407, 403), (408, 404), (119, 401), (999, 503)]
)
def test_file_permission_errors_are_sanitized_and_do_not_revoke_app_account(files, code, status):
    client, download, _, _ = files
    download.code = code
    response = client.get("/v1/files/resources/test.pdf")
    assert response.status_code == status
    assert "fixture" not in response.text and "/approved" not in response.text
    assert client.get("/v1/datasets").status_code == 200


def test_logout_and_missing_bearer_prevent_upstream_download(files):
    client, download, account, _ = files
    client.headers.pop("Authorization")
    assert client.get("/v1/files/updates/zheri.apk").status_code == 401
    client.headers["Authorization"] = "Bearer " + account["access_token"]
    assert (
        client.post("/v1/auth/logout", json={"refresh_token": account["refresh_token"]}).status_code
        == 200
    )
    assert client.get("/v1/files/updates/zheri.apk").status_code == 401
    assert not download.seen


def test_each_app_account_uses_its_own_dsm_session(files):
    client, download, alice, dsm = files
    client.get("/v1/files/updates/zheri.apk")
    first_sid = download.seen[-1].headers["cookie"].removeprefix("id=")
    bob = login(client, "bob")
    assert bob["user_id"] != alice["user_id"]
    client.headers["Authorization"] = "Bearer " + bob["access_token"]
    client.get("/v1/files/updates/zheri.apk")
    second_sid = download.seen[-1].headers["cookie"].removeprefix("id=")
    assert dsm.sessions[first_sid] != dsm.sessions[second_sid]


def test_valid_resume_and_upstream_ignoring_range_are_forwarded_correctly(files):
    client, download, _, _ = files
    download.status, download.body = 206, b"age"
    download.headers = {"Content-Range": "bytes 4-6/7"}
    response = client.get("/v1/files/updates/zheri.apk", headers={"Range": "bytes=4-"})
    assert response.status_code == 206 and response.content == b"age"
    assert response.headers["content-range"] == "bytes 4-6/7"
    assert download.seen[-1].headers["range"] == "bytes=4-"
    download.status, download.body, download.headers = 200, b"package", {}
    response = client.get("/v1/files/updates/zheri.apk", headers={"Range": "bytes=4-"})
    assert response.status_code == 200 and response.content == b"package"


@pytest.mark.parametrize("header", ["bytes=1-2", "bytes=-2", "bytes=0-,2-", "bytes=1073741824-"])
def test_invalid_ranges_rejected_before_nas(files, header):
    client, download, _, _ = files
    assert client.get("/v1/files/updates/zheri.apk", headers={"Range": header}).status_code == 416
    assert not download.seen


@pytest.mark.parametrize(
    "content_range", ["bad", "bytes 3-6/7", "bytes 4-8/7", "bytes 4-6/9999999999"]
)
def test_invalid_upstream_ranges_rejected_without_using_payload(files, content_range):
    client, download, _, _ = files
    download.status, download.body = 206, b"age"
    download.headers = {"Content-Range": content_range}
    # Repeating failures also proves transfer slots are released.
    for _ in range(5):
        response = client.get("/v1/files/updates/zheri.apk", headers={"Range": "bytes=4-"})
        assert response.status_code == 503


@pytest.mark.parametrize(
    "path", ["../secret", "/root", "a//b", "a/%2e%2e/b", "a?x", "a\\b", "a\x01b", ""]
)
def test_relative_paths_reject_escape_and_double_encoding(path):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        relative_path(path)


def test_invalid_manifest_never_becomes_a_client_path(files):
    client, download, _, _ = files
    download.manifest["artifacts"][0]["path"] = "../secret"
    response = client.get("/v1/updates/manifest")
    assert response.status_code == 503 and response.json()["detail"] == "invalid_update_manifest"
    assert client.get("/v1/files/private/test").status_code == 503

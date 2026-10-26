import hashlib
import importlib
import io
import json
import tarfile
from pathlib import Path

import pytest


def test_container_validation_uses_current_application_contract(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    verifier = importlib.import_module("verify_stack")
    configuration = verifier.verification_stack("app-data-service:fixture")
    api = configuration["services"]["api"]
    settings = json.loads(api["environment"]["APP_DATA_APPLICATIONS"])
    assert settings["zheri"]["file_roots"]["updates"] == "/fixture/updates"
    assert settings["zheri"]["browser_origin"] != settings["board_game_agent"]["browser_origin"]
    assert "APP_DATA_BROWSER_ORIGIN" not in api["environment"]
    assert "APP_DATA_UPDATES_ROOT" not in api["environment"]
    assert "app-data register-applications" in api["command"][2]
    bundler = importlib.import_module("build_docker_bundle")
    bundler.validate_api_configuration(configuration)
    del api["environment"]["APP_DATA_APPLICATIONS"]
    with pytest.raises(ValueError, match="APP_DATA_APPLICATIONS"):
        bundler.validate_api_configuration(configuration)


@pytest.mark.parametrize(
    "fault", [None, "oci", "wrong-tag", "wrong-platform", "wrong-id", "missing-layer"]
)
def test_image_delivery_rejects_incorrect_or_incomplete_packages(tmp_path, monkeypatch, fault):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    delivery = importlib.import_module("build_docker_bundle")
    config = json.dumps(
        {"os": "linux", "architecture": "arm64" if fault == "wrong-platform" else "amd64"}
    ).encode()
    identity = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = [
        {
            "Config": "config.json",
            "RepoTags": ["wrong:1" if fault == "wrong-tag" else "test:1"],
            "Layers": ["layer.tar"],
        }
    ]
    path = tmp_path / "test.tar.gz"
    files = {"config.json": config, "manifest.json": json.dumps(manifest).encode()}
    if fault == "oci":
        descriptor = json.dumps({"config": {"digest": identity}}).encode()
        identity = "sha256:" + hashlib.sha256(descriptor).hexdigest()
        files["blobs/sha256/" + identity.removeprefix("sha256:")] = descriptor
    if fault != "missing-layer":
        files["layer.tar"] = b"fixture layer"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    expected_identity = "sha256:" + "0" * 64 if fault == "wrong-id" else identity
    if fault and fault != "oci":
        with pytest.raises((ValueError, KeyError)):
            delivery.validate_archive(path, "test:1", expected_identity)
    else:
        delivery.validate_archive(path, "test:1", expected_identity)

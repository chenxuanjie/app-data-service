import importlib
import io
import json
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def bundler(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("service_web_bundle")


def bundle(path, extra=None):
    with tarfile.open(path, "w:gz") as archive:
        files = {
            "index.html": b"shell",
            "main.dart.js": b"script",
            "flutter_bootstrap.js": b"load",
            "version.json": b'{"app_name":"zheri","version":"0.13.0","build_number":"64"}',
        }
        if extra:
            files.update(extra)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_web_archive_retains_assets_and_version(bundler, tmp_path):
    archive = tmp_path / "web.tar.gz"
    bundle(archive, {"assets/fonts/test.ttf": b"font"})
    destination = tmp_path / "extracted"
    version = bundler.extract_web(archive, destination)
    assert version == {"version": "0.13.0", "buildNumber": "64"}
    assert (destination / "assets/fonts/test.ttf").read_bytes() == b"font"


@pytest.mark.parametrize("name", ["../secret", "/absolute", "a\\secret", "C:/secret"])
def test_web_archive_cannot_write_outside_build_context(bundler, tmp_path, name):
    archive = tmp_path / "web.tar.gz"
    bundle(archive, {name: b"secret"})
    with pytest.raises(ValueError):
        bundler.extract_web(archive, tmp_path / "extracted")


def test_web_archive_cannot_contain_a_symlink(bundler, tmp_path):
    archive = tmp_path / "web.tar.gz"
    with tarfile.open(archive, "w:gz") as source:
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "../secret"
        source.addfile(link)
    with pytest.raises(ValueError):
        bundler.extract_web(archive, tmp_path / "extracted")


def test_build_input_verifies_source_gate_and_binds_archive_hash(bundler, monkeypatch, tmp_path):
    archive, receipt = tmp_path / "web.tar.gz", tmp_path / "source.json"
    bundle(archive)
    receipt.write_text(json.dumps({"sources": [{"package": "zheri", "commit": "fixture"}]}))
    calls = []
    monkeypatch.setattr(
        bundler.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs))
    )
    record = bundler.verify_web(tmp_path, archive, receipt, tmp_path / "gate.py")
    assert record["sources"][0]["commit"] == "fixture" and len(record["sha256"]) == 64
    assert "verify" in calls[0][0] and calls[0][1]["check"]
    assert str(archive) in calls[0][0]

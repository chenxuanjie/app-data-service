import copy
import hashlib
import importlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("archive_cache"), importlib.import_module(
        "docker_archive_worker"
    )


@pytest.mark.parametrize(
    "fault", [None, "id", "platform", "proof", "size", "hash", "missing", "name"]
)
def test_dependency_reuse_requires_identity_and_unchanged_verified_bytes(modules, tmp_path, fault):
    cache, _ = modules
    path = tmp_path / "postgresql-17.11.tar.gz"
    payload = b"previously validated archive bytes"
    path.write_bytes(payload)
    record = {
        "file": path.name,
        "image": "postgres:17.11-bookworm",
        "imageId": "sha256:old",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "importVerified": True,
    }
    previous = {"platform": "linux/amd64", "archives": [copy.deepcopy(record)]}
    if fault == "id":
        previous["archives"][0]["imageId"] = "sha256:new"
    elif fault == "platform":
        previous["platform"] = "linux/arm64"
    elif fault == "proof":
        previous["archives"][0]["importVerified"] = False
    elif fault == "size":
        path.write_bytes(payload + b"changed")
    elif fault == "hash":
        path.write_bytes(b"X" + payload[1:])
    elif fault == "missing":
        path.unlink()
    elif fault == "name":
        previous["archives"][0]["file"] = "../outside.tar.gz"
    found, _ = cache.cached_dependency(
        previous, tmp_path, record["image"], record["imageId"], path.name
    )
    assert bool(found) == (fault is None)
    if found:
        assert found == record and found is not previous["archives"][0]


def raw_archive(path):
    config = json.dumps({"os": "linux", "architecture": "amd64"}).encode()
    identity = "sha256:" + hashlib.sha256(config).hexdigest()
    files = {"config.json": config, "layer.tar": b"image payload"}
    files["manifest.json"] = json.dumps(
        [{"Config": "config.json", "RepoTags": ["sample:1"], "Layers": ["layer.tar"]}]
    ).encode()
    with tarfile.open(path, "w") as archive:
        for name, value in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            archive.addfile(entry, io.BytesIO(value))
    return identity


@pytest.mark.parametrize("fault", [None, "load", "wrong-id", "copy-corruption"])
def test_archive_worker_keeps_large_intermediates_local_and_cleans_failure(
    modules, tmp_path, monkeypatch, fault
):
    _, worker = modules
    local = tmp_path / "linux-local"
    local.mkdir()
    destination = tmp_path / "windows-staging/result.tar.gz"
    fixture = tmp_path / "fixture.tar"
    identity = raw_archive(fixture)
    commands, copies = [], []

    def run(command, **kwargs):
        commands.append(command)
        if "save" in command:
            path = Path(command[command.index("--output") + 1])
            assert path.is_relative_to(local)
            path.write_bytes(fixture.read_bytes())
        else:
            assert "load" in command
            assert Path(command[-1]).is_relative_to(local)
            if fault == "load":
                raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(
        worker.subprocess,
        "check_output",
        lambda *a, **k: "sha256:wrong" if fault == "wrong-id" else identity,
    )
    real_copy = worker.shutil.copyfile

    def copy_file(source, target):
        copies.append((Path(source), Path(target)))
        assert Path(source).is_relative_to(local)
        result = real_copy(source, target)
        if fault == "copy-corruption":
            Path(target).write_bytes(b"corrupted")
        return result

    monkeypatch.setattr(worker.shutil, "copyfile", copy_file)
    if fault:
        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            worker.export_image("sample:1", identity, destination, workspace_parent=local)
        assert not destination.exists()
    else:
        result = worker.export_image("sample:1", identity, destination, workspace_parent=local)
        assert result["importVerified"] and worker.archive_digest(destination) == result["sha256"]
        assert len(copies) == 1 and copies[0][1] == destination
    assert not list(local.iterdir())


def test_worker_does_not_overwrite_an_existing_deliverable(modules, tmp_path):
    _, worker = modules
    target = tmp_path / "existing.tar.gz"
    target.write_bytes(b"existing deliverable")
    with pytest.raises(ValueError, match="new staging"):
        worker.export_image("sample:1", "unused", target)
    assert target.read_bytes() == b"existing deliverable"

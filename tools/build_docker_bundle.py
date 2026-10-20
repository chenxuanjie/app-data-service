"""Build, save, load and verify a fixed Synology image-import TAR locally.

Python backend project: the Flutter publisher's pubspec-only wrapper does not
apply. This driver keeps the same build/save/load/verify and atomic-delivery
contract without registering a fake Flutter project or touching NAS.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    parser.add_argument(
        "--docker", choices=["native", "wsl"], default="wsl" if os.name == "nt" else "native"
    )
    args = parser.parse_args()
    docker = ["wsl", "-d", "Ubuntu", "--exec", "docker"] if args.docker == "wsl" else ["docker"]

    def linux(path):
        return (
            subprocess.check_output(
                ["wsl", "-d", "Ubuntu", "--exec", "wslpath", "-a", str(path)], text=True, timeout=10
            ).strip()
            if args.docker == "wsl"
            else str(path)
        )

    def run(command, **kwargs):
        return subprocess.run(command, check=True, cwd=ROOT, **kwargs)

    configuration = yaml.safe_load(args.compose.read_text(encoding="utf-8"))
    if set(configuration["services"]) != {"postgres", "migrate", "api", "powersync"}:
        raise ValueError("Use the generated four-service test Compose")
    if any(
        key in service
        for service in configuration["services"].values()
        for key in ["build", "env_file"]
    ):
        raise ValueError("Deployment must be self-contained")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    image = f"app-data-service:{version}-{revision[:7]}{'-dirty' if dirty else ''}-{stamp}"
    staging = ROOT / ".local" / ("bundle-" + uuid4().hex[:12])
    staging.mkdir(parents=True)
    wheels = ROOT / ".docker-wheels"
    wheels.mkdir(exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheels),
            "--only-binary=:all:",
            "--no-deps",
            "--timeout",
            "15",
            "--retries",
            "1",
            "--platform",
            "manylinux_2_17_x86_64",
            "--platform",
            "manylinux_2_28_x86_64",
            "--platform",
            "manylinux_2_34_x86_64",
            "--platform",
            "manylinux2014_x86_64",
            "--python-version",
            "3.14",
            "--implementation",
            "cp",
            "--abi",
            "cp314",
            "--abi",
            "abi3",
            "--abi",
            "none",
            "-r",
            str(ROOT / "requirements-runtime.lock"),
        ],
        timeout=180,
    )
    run(
        [
            *docker,
            "buildx",
            "build",
            "--load",
            "--pull=false",
            "--provenance=false",
            "--sbom=false",
            "--platform",
            "linux/amd64",
            "--label",
            "org.opencontainers.image.revision=" + revision,
            "--label",
            "org.opencontainers.image.version=" + version,
            "--tag",
            image,
            "--file",
            linux(ROOT / "Dockerfile"),
            linux(ROOT),
        ],
        timeout=240,
    )
    images = [image, "postgres:17.11-bookworm", "journeyapps/powersync-service:1.27.0"]
    inspected = json.loads(
        subprocess.check_output([*docker, "image", "inspect", *images], text=True, timeout=15)
    )
    if any(item["Os"] != "linux" or item["Architecture"] != "amd64" for item in inspected):
        raise RuntimeError("Unexpected image platform")
    archive = staging / "images.tar"
    run([*docker, "image", "save", "--output", linux(archive), *images], timeout=180)
    with tarfile.open(archive) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
    archived_tags = {tag for entry in manifest for tag in entry.get("RepoTags", [])}
    if not set(images) <= archived_tags:
        raise RuntimeError("Archive is missing an image")
    run([*docker, "image", "load", "--input", linux(archive)], timeout=180)
    run(
        [
            sys.executable,
            str(ROOT / "tools/verify_stack.py"),
            "--api-image",
            image,
            "--docker",
            args.docker,
            "--dart",
            args.dart,
            "--report",
            str(staging / "validation.json"),
        ],
        timeout=360,
    )
    for name in ["api", "migrate"]:
        configuration["services"][name]["image"] = image
    for service in configuration["services"].values():
        service["pull_policy"] = "never"
    rendered = staging / "docker-compose.yml"
    rendered.write_text(
        yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # Never print the resolved Compose: it contains deployment secrets.
    with (staging / "compose-validation.json").open("w", encoding="utf-8") as private:
        run(
            [*docker, "compose", "-f", linux(rendered), "config", "--format", "json"],
            stdout=private,
            timeout=20,
        )
    source_hash = hashlib.sha256()
    sources = [
        ROOT / name
        for name in ["Dockerfile", "pyproject.toml", "alembic.ini", "requirements-runtime.lock"]
    ]
    sources += sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "migrations").rglob("*.py"))
    for path in sources:
        source_hash.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    with archive.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    metadata = {
        "version": version,
        "sourceRevision": revision,
        "dirtySource": dirty,
        "sourceTreeSha256": source_hash.hexdigest(),
        "platform": "linux/amd64",
        "images": images,
        "imageIds": [item["Id"] for item in inspected],
        "size": archive.stat().st_size,
        "sha256": digest,
        "builtAt": datetime.now(UTC).isoformat(),
        "realNasDeployed": False,
    }
    output = ROOT / "release"
    reports = output / "reports/docker-linux-amd64"
    reports.mkdir(parents=True, exist_ok=True)
    # Replace the deliverable only after archive import and live validation pass.
    archive.replace(output / "app-data-service-linux-amd64-images.tar")
    for name in ["docker-compose.yml", "validation.json"]:
        shutil.copyfile(staging / name, reports / name)
    (reports / "release.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (reports / "checksums.sha256").write_text(
        digest + "  ../../app-data-service-linux-amd64-images.tar\n", encoding="utf-8"
    )
    print(json.dumps(metadata), flush=True)


if __name__ == "__main__":
    main()

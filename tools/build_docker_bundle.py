"""Build or repack three compressed Synology image archives locally.

Python backend project: the Flutter publisher's pubspec-only wrapper does not
apply. This driver keeps the same build/save/load/verify and atomic-delivery
contract without registering a fake Flutter project or touching NAS.
"""

import argparse
import base64
import gzip
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
from service_web_bundle import extract_web, verify_web

ROOT = Path(__file__).resolve().parents[1]


def source_fingerprint():
    digest = hashlib.sha256()
    sources = [
        ROOT / name
        for name in ["Dockerfile", "pyproject.toml", "alembic.ini", "requirements-runtime.lock"]
    ]
    sources += sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "migrations").rglob("*.py"))
    for path in sources:
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def validate_archive(path, expected_tag, expected_image_id):
    """Reject wrong images/platforms and incomplete archives before delivery."""
    with tarfile.open(path, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        if len(manifest) != 1 or manifest[0].get("RepoTags") != [expected_tag]:
            raise ValueError("Archive must contain exactly its designated image tag")
        entry = manifest[0]
        config_bytes = archive.extractfile(entry["Config"]).read()
        config = json.loads(config_bytes)
        if config["os"] != "linux" or config["architecture"] != "amd64":
            raise ValueError("Unexpected archived image platform")
        config_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()

        def references_config(identity, visited):
            if identity == config_id:
                return True
            if identity in visited or not identity.startswith("sha256:"):
                return False
            visited.add(identity)
            try:
                payload = archive.extractfile(
                    "blobs/sha256/" + identity.removeprefix("sha256:")
                ).read()
            except KeyError:
                return False
            if "sha256:" + hashlib.sha256(payload).hexdigest() != identity:
                return False
            descriptor = json.loads(payload)
            if descriptor.get("config", {}).get("digest") == config_id:
                return True
            return any(
                references_config(child["digest"], visited)
                for child in descriptor.get("manifests", [])
            )

        # Classic IDs identify configs; Docker 29's containerd store can instead
        # identify an OCI manifest/index. Bind either to the same image config.
        if not references_config(expected_image_id, set()):
            raise ValueError("Archived image identity differs from inspected image")
        if not entry["Layers"]:
            raise ValueError("Image layers are missing")
        for layer in entry["Layers"]:
            if not archive.getmember(layer).isfile():
                raise ValueError("Image layer is missing")


def update_sync_rules(configuration: dict, content: str) -> None:
    """Update rules only; never regenerate database passwords or signing keys."""
    environment = configuration["services"]["powersync"]["environment"]
    decoded = yaml.safe_load(base64.b64decode(environment["POWERSYNC_CONFIG_B64"], validate=True))
    decoded["sync_config"]["content"] = content
    environment["POWERSYNC_CONFIG_B64"] = base64.b64encode(
        yaml.safe_dump(decoded, sort_keys=False, allow_unicode=True).encode()
    ).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    parser.add_argument("--web-project", type=Path, required=True)
    parser.add_argument("--web-archive", type=Path, required=True)
    parser.add_argument("--web-receipt", type=Path, required=True)
    parser.add_argument("--source-gate", type=Path, required=True)
    parser.add_argument(
        "--reuse-existing",
        type=Path,
        help="Repack an already validated release.json without rebuilding images",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Deliver only the API image; dependency images must already be installed",
    )
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
    if set(configuration["services"]) != {"postgres", "api", "powersync"}:
        raise ValueError("Use the current three-service test Compose")
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
    web_input = verify_web(args.web_project, args.web_archive, args.web_receipt, args.source_gate)
    web_directory = staging / "web"
    web_input.update(extract_web(args.web_archive, web_directory))
    original = None
    if args.reuse_existing:
        original = json.loads(args.reuse_existing.read_text(encoding="utf-8"))
        image = original["images"][0]
        if source_fingerprint() != original["sourceTreeSha256"]:
            raise ValueError(
                "Runtime sources differ from the release; rebuild instead of repacking"
            )
        if web_input != original.get("web"):
            raise ValueError("Web sources differ; rebuild instead of repacking")
        version = original["version"]
    else:
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
                "--build-context",
                "web_assets=" + linux(web_directory),
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
    image_ids = [item["Id"] for item in inspected]
    if original and image_ids != original["imageIds"]:
        raise ValueError("Existing image IDs no longer match the validated release")
    delivery = staging / "delivery"
    delivery.mkdir()
    details = delivery / "metadata"
    details.mkdir()
    archives = []
    names = ["app-data-service-api", "postgres", "powersync"]
    filenames = [
        f"fastapi-{version}.tar.gz",
        "postgresql-" + images[1].rsplit(":", 1)[1].split("-", 1)[0] + ".tar.gz",
        "powersync-" + images[2].rsplit(":", 1)[1] + ".tar.gz",
    ]
    for index, (name, tag, identity) in enumerate(zip(names, images, image_ids, strict=True)):
        if args.api_only and index != 0:
            continue
        raw = staging / (name + ".tar")
        filename = filenames[index]
        compressed = delivery / filename
        print("Exporting " + name, flush=True)
        run([*docker, "image", "save", "--output", linux(raw), tag], timeout=180)
        with raw.open("rb") as source, compressed.open("wb") as destination:
            with gzip.GzipFile(filename="", fileobj=destination, mode="wb", mtime=0) as zipped:
                shutil.copyfileobj(source, zipped, length=4 * 1024 * 1024)
        validate_archive(compressed, tag, identity)
        run([*docker, "image", "load", "--input", linux(compressed)], timeout=180)
        loaded = subprocess.check_output(
            [*docker, "image", "inspect", "--format", "{{.Id}}", tag], text=True, timeout=15
        ).strip()
        if loaded != identity:
            raise RuntimeError("Loaded image identity differs from the original")
        with compressed.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        archives.append(
            {
                "component": name,
                "file": filename,
                "image": tag,
                "imageId": identity,
                "size": compressed.stat().st_size,
                "sha256": digest,
                "importVerified": True,
            }
        )
        raw.unlink()
    # Run against the images imported from the compressed archives, not the old TAR.
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
            str(details / "validation.json"),
        ],
        timeout=360,
    )
    # Recheck the Flutter repository/path packages and archive after image validation.
    if verify_web(args.web_project, args.web_archive, args.web_receipt, args.source_gate) != {
        key: web_input[key] for key in ("archive", "sha256", "sources")
    }:
        raise ValueError("Web input changed during image build")
    if original is None:
        update_sync_rules(
            configuration, (ROOT / "deploy/powersync/sync-config.yaml").read_text(encoding="utf-8")
        )
    configuration["services"]["api"]["image"] = image
    for service in configuration["services"].values():
        service["pull_policy"] = "never"
    rendered = delivery / "docker-compose.yml"
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
    updated_archives = [item["file"] for item in archives]
    output = ROOT / "release"
    previous_path = output / "metadata/release.json"
    previous = (
        json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else None
    )
    if args.api_only:
        # Keep a complete, current install set locally while marking only API
        # for transfer. Reuse dependencies only when their IDs and hashes match.
        if previous is None:
            raise ValueError("Create a full delivery before an API-only update")
        for tag, identity in zip(images[1:], image_ids[1:], strict=True):
            record = next(item for item in previous["archives"] if item["image"] == tag)
            if record["imageId"] != identity:
                raise ValueError("Dependency changed; create a full delivery instead")
            with (output / record["file"]).open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != record["sha256"]:
                    raise ValueError("Cached dependency archive checksum mismatch")
            archives.append(record)
    metadata = {
        "schemaVersion": 2,
        "deliveryKind": "api-update" if args.api_only else "first-install",
        "version": version,
        "sourceRevision": original["sourceRevision"] if original else revision,
        "dirtySource": original["dirtySource"] if original else dirty,
        "sourceTreeSha256": source_fingerprint(),
        "packagingRevision": revision,
        "dirtyPackaging": dirty,
        "repackedFrom": str(args.reuse_existing.resolve()) if original else None,
        "platform": "linux/amd64",
        "web": web_input,
        "images": images,
        "imageIds": image_ids,
        "archives": archives,
        "updatedArchives": updated_archives,
        "totalSize": sum(item["size"] for item in archives),
        "packagedAt": datetime.now(UTC).isoformat(),
        "realNasDeployed": False,
    }
    (details / "release.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (details / "checksums.sha256").write_text(
        "".join(item["sha256"] + "  " + item["file"] + "\n" for item in archives),
        encoding="utf-8",
    )
    shutil.copyfile(ROOT / "docs/release-guide.md", delivery / "README.md")
    output.mkdir(parents=True, exist_ok=True)
    # Promote only after every compressed archive and the live stack have passed.
    for path in delivery.rglob("*"):
        if path.is_file():
            target = output / path.relative_to(delivery)
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
    # Remove only superseded archives owned by the previous delivery record.
    # Leave unknown files or a modified archive untouched.
    current_names = {record["file"] for record in archives}
    for record in previous["archives"] if previous else []:
        old = output / record["file"]
        if (
            record["file"] in current_names
            or old.parent.resolve() != output.resolve()
            or not old.name.endswith(".tar.gz")
            or not old.is_file()
        ):
            continue
        with old.open("rb") as file:
            unchanged = hashlib.file_digest(file, "sha256").hexdigest() == record["sha256"]
        if unchanged:
            old.unlink()
    print(json.dumps(metadata), flush=True)


if __name__ == "__main__":
    main()

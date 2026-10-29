"""Build or repack three compressed Synology image archives locally.

Python backend project: the Flutter publisher's pubspec-only wrapper does not
apply. This driver keeps the same build/save/load/verify and atomic-delivery
contract without registering a fake Flutter project or touching NAS.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml
from archive_cache import cached_dependency
from configure_public_releases import validate_delivery as validate_release_delivery
from docker_runtime import docker_runtime
from image_archives import validate_archive as validate_archive
from render_stack import remove_board_library_startup
from render_sync_config import render as render_sync_config
from task_diagnostics import TaskLog

from app_data_service.modules.registry import installed_applications

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


def update_sync_rules(configuration: dict, content: str) -> None:
    """Update rules only; never regenerate database passwords or signing keys."""
    environment = configuration["services"]["powersync"]["environment"]
    decoded = yaml.safe_load(base64.b64decode(environment["POWERSYNC_CONFIG_B64"], validate=True))
    decoded["sync_config"]["content"] = content
    environment["POWERSYNC_CONFIG_B64"] = base64.b64encode(
        yaml.safe_dump(decoded, sort_keys=False, allow_unicode=True).encode()
    ).decode()


def validate_api_configuration(configuration):
    environment = configuration["services"]["api"]["environment"]
    if "APP_DATA_APPLICATIONS" not in environment or "APP_DATA_WEB_DIRECTORY" in environment:
        raise ValueError("Pure API delivery requires current APP_DATA_APPLICATIONS configuration")
    installed_applications().validate_settings(json.loads(environment["APP_DATA_APPLICATIONS"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    parser.add_argument(
        "--full-validation",
        action="store_true",
        help="Opt in to full business/SDK acceptance; default is artifact and startup checks",
    )
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
    parser.add_argument(
        "--force-reexport",
        action="store_true",
        help="Ignore dependency archive cache and re-export all images",
    )
    args = parser.parse_args()
    if args.api_only and args.force_reexport:
        parser.error("--force-reexport requires a full bundle, not --api-only")
    log = TaskLog(ROOT, "docker-build")
    try:
        with docker_runtime(args.docker):
            if args.docker == "wsl":
                log.run(
                    "local-environment-ready",
                    [
                        sys.executable,
                        str(ROOT / "tools/local_environment.py"),
                        "--purpose",
                        "build",
                    ],
                    timeout=900,
                    cwd=ROOT,
                )
            build(args, log=log)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        print(f"Build stopped: {type(error).__name__}. See {log.directory}", file=sys.stderr)
        return 1
    return 0


def build(args, log=None):
    log = log or TaskLog(ROOT, "docker-build")
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
        # Only executable/subcommand names go into diagnostics; arguments can
        # contain private paths. Resolved Compose output keeps its private sink.
        if "buildx" in command:
            name = "image-build" if "build" in command else "buildx-preflight"
        elif "pip" in command:
            name = "dependency-download"
        elif any("build_preflight.py" in str(part) for part in command):
            name = "proxy-registry-preflight"
        elif any("docker_archive_worker.py" in str(part) for part in command):
            name = "image-archive"
        elif "save" in command:
            name = "image-export"
        elif "load" in command:
            name = "image-import"
        elif str(ROOT / "tools/verify_stack.py") in command:
            name = "stack-validation"
        else:
            name = "docker-preflight" if "info" in command else "compose-validation"
        return log.run(name, command, timeout=kwargs.pop("timeout", 120), cwd=ROOT, **kwargs)

    configuration = yaml.safe_load(args.compose.read_text(encoding="utf-8-sig"))
    if set(configuration["services"]) != {"postgres", "api", "powersync"}:
        raise ValueError("Use the current three-service test Compose")
    if any(
        key in service
        for service in configuration["services"].values()
        for key in ["build", "env_file"]
    ):
        raise ValueError("Deployment must be self-contained")
    validate_api_configuration(configuration)
    validate_release_delivery(configuration)

    run([*docker, "info", "--format", "{{.OSType}}"], timeout=120)
    run([*docker, "buildx", "version"], timeout=120)
    network_script = ROOT / "tools/build_preflight.py"
    network_command = (
        [*docker[:-1], "python3", linux(network_script)]
        if args.docker == "wsl"
        else [sys.executable, str(network_script)]
    )
    run(network_command, timeout=180)

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    image = f"app-data-service:{version}-{revision[:7]}{'-dirty' if dirty else ''}-{stamp}"
    staging = ROOT / ".local" / ("bundle-" + uuid4().hex[:12])
    staging.mkdir(parents=True)
    original = None
    if args.reuse_existing:
        original = json.loads(args.reuse_existing.read_text(encoding="utf-8"))
        image = original["images"][0]
        if source_fingerprint() != original["sourceTreeSha256"]:
            raise ValueError(
                "Runtime sources differ from the release; rebuild instead of repacking"
            )
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
            timeout=3600,
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
            timeout=14400,
        )
    images = [image, "postgres:17.11-bookworm", "journeyapps/powersync-service:1.27.0"]
    inspected = json.loads(
        subprocess.check_output([*docker, "image", "inspect", *images], text=True, timeout=120)
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
    output = ROOT / "release"
    previous_path = output / "metadata/release.json"
    previous = (
        json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else None
    )
    reused_archives, rebuilt_archives, archive_actions = [], [], []
    for index, (name, tag, identity) in enumerate(zip(names, images, image_ids, strict=True)):
        filename = filenames[index]
        started = time.monotonic()
        record, reason = (
            None,
            "forced dependency re-export" if index != 0 else "API always exported",
        )
        if index != 0 and not args.force_reexport:
            record, reason = cached_dependency(previous, output, tag, identity, filename)
        if record:
            print("Reusing validated " + name + " archive: " + reason, flush=True)
            archives.append(record)
            reused_archives.append(filename)
            archive_actions.append(
                {
                    "component": name,
                    "action": "reused",
                    "reason": reason,
                    "seconds": round(time.monotonic() - started, 3),
                }
            )
            continue
        if args.api_only and index != 0:
            raise ValueError(
                "Dependency cache is unavailable: " + reason + "; create a full bundle"
            )
        print("Exporting " + name + ": " + reason, flush=True)
        compressed = delivery / filename
        worker_report = staging / (name + "-archive.json")
        worker = ROOT / "tools/docker_archive_worker.py"
        worker_command = (
            [*docker[:-1], "python3", linux(worker)]
            if args.docker == "wsl"
            else [sys.executable, str(worker)]
        )
        run(
            [
                *worker_command,
                "--image",
                tag,
                "--identity",
                identity,
                "--destination",
                linux(compressed),
                "--report",
                linux(worker_report),
            ],
            timeout=10800,
        )
        result = json.loads(worker_report.read_text(encoding="utf-8"))
        archives.append(
            {
                "component": name,
                "file": filename,
                "image": tag,
                "imageId": identity,
                "size": result["size"],
                "sha256": result["sha256"],
                "importVerified": True,
            }
        )
        rebuilt_archives.append(filename)
        archive_actions.append(
            {
                "component": name,
                "action": "exported",
                "reason": reason,
                "seconds": round(time.monotonic() - started, 3),
                "workspace": result["workspace"],
                "timings": result["timings"],
            }
        )
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
            *([] if args.full_validation else ["--smoke-only"]),
        ],
        timeout=14400,
    )
    if original is None:
        update_sync_rules(configuration, render_sync_config())
    configuration["services"]["api"]["image"] = image
    remove_board_library_startup(configuration)
    for service in configuration["services"].values():
        service["pull_policy"] = "never"
    rendered = delivery / "docker-compose.yml"
    validate_release_delivery(configuration)
    rendered.write_text(
        yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    # Never print the resolved Compose: it contains deployment secrets.
    with (staging / "compose-validation.json").open("w", encoding="utf-8") as private:
        run(
            [*docker, "compose", "-f", linux(rendered), "config", "--format", "json"],
            stdout=private,
            timeout=120,
        )
    updated_archives = rebuilt_archives if args.api_only else [item["file"] for item in archives]
    metadata = {
        "schemaVersion": 2,
        "composePath": str(output / "docker-compose.yml"),
        "deliveryKind": "api-update" if args.api_only else "first-install",
        "version": version,
        "sourceRevision": original["sourceRevision"] if original else revision,
        "dirtySource": original["dirtySource"] if original else dirty,
        "sourceTreeSha256": source_fingerprint(),
        "packagingRevision": revision,
        "dirtyPackaging": dirty,
        "repackedFrom": str(args.reuse_existing.resolve()) if original else None,
        "platform": "linux/amd64",
        "runtime": "api-only",
        "images": images,
        "imageIds": image_ids,
        "archives": archives,
        "updatedArchives": updated_archives,
        "reusedArchives": reused_archives,
        "rebuiltArchives": rebuilt_archives,
        "archiveActions": archive_actions,
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
    # Recheck reused deliverables before promotion in case another task changed them.
    for record in archives:
        if record["file"] in reused_archives:
            verified, _ = cached_dependency(
                previous, output, record["image"], record["imageId"], record["file"]
            )
            if verified is None:
                raise ValueError("Reused archive changed during build; previous delivery preserved")
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
    compose_path = (output / "docker-compose.yml").as_posix()
    print(f"Compose: [docker-compose.yml]({compose_path})", flush=True)


if __name__ == "__main__":
    sys.exit(main())

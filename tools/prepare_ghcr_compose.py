"""Prepare a private GHCR Compose only for the exact locally verified image."""

import argparse
import json
import re
import subprocess
from pathlib import Path

import yaml
from configure_public_releases import validate_delivery

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/chenxuanjie/app-data-service"


def render(configuration, image, revision, inspection, validation):
    if not re.fullmatch(re.escape(IMAGE) + r"@sha256:[a-f0-9]{64}", image):
        raise ValueError("Use this repository's exact GHCR digest")
    if (inspection.get("os"), inspection.get("architecture")) != ("linux", "amd64"):
        raise ValueError("Expected a linux/amd64 API image")
    labels = inspection.get("labels", {})
    if (
        labels.get("org.opencontainers.image.revision") != revision
        or labels.get("org.opencontainers.image.source")
        != "https://github.com/chenxuanjie/app-data-service"
    ):
        raise ValueError("GHCR image does not match the committed repository source")
    if image not in inspection.get("digests", []):
        raise ValueError("Local image identity differs from requested digest")
    required = ("sqlPublication", "anonymousDownloadSha256", "persistedAfterRestart")
    if (
        validation.get("apiImage") != image
        or not validation.get("non_root_release_volume_permissions")
        or not all(validation.get("publicRelease", {}).get(field) for field in required)
    ):
        raise ValueError("This image has not passed isolated public release verification")
    validate_delivery(configuration)
    configuration["services"]["api"]["image"] = image
    configuration["services"]["api"]["pull_policy"] = "always"
    return configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", choices=["wsl", "native"], default="wsl")
    args = parser.parse_args()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=15
    ).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, timeout=15):
        parser.error("Commit reviewed source changes before preparing GHCR delivery")
    docker = ["wsl", "-d", "Ubuntu", "--exec", "docker"] if args.docker == "wsl" else ["docker"]
    template = (
        '{"os":{{json .Os}},"architecture":{{json .Architecture}},'
        '"labels":{{json .Config.Labels}},"digests":{{json .RepoDigests}}}'
    )
    inspection = json.loads(
        subprocess.check_output(
            [*docker, "image", "inspect", "--format", template, args.image], text=True, timeout=30
        )
    )
    configuration = render(
        yaml.safe_load(args.compose.read_text(encoding="utf-8-sig")),
        args.image,
        revision,
        inspection,
        json.loads(args.validation.read_text(encoding="utf-8")),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write("# GHCR API delivery; preserve the existing NAS project and data volumes.\n")
        yaml.safe_dump(configuration, output, allow_unicode=True, sort_keys=False)
    temporary.replace(args.output)
    print("Prepared private GHCR Compose: " + str(args.output.resolve()))


if __name__ == "__main__":
    main()

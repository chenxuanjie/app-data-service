"""Prepare an existing private Compose and publisher configuration; never contact NAS."""

import argparse
import hashlib
import json
import re
import secrets
from pathlib import Path, PurePosixPath

import yaml

from app_data_service.modules.registry import installed_applications


def validate_delivery(configuration):
    """Check selected public-content apps before a release bundle claims readiness."""
    api = configuration["services"]["api"]
    env = api["environment"]
    settings = json.loads(env.get("APP_DATA_APPLICATIONS", "{}"))
    keys = json.loads(env.get("APP_DATA_RELEASE_PUBLISH_KEY_HASHES", "{}"))
    if not isinstance(keys, dict):
        raise ValueError("Application release publishing hashes must be an object")
    registry = installed_applications()
    registry.validate_settings(settings)
    required = {
        app_id
        for app_id, value in settings.items()
        if value.get("public_file_roots")
        and registry.get(app_id).capabilities.get("public_releases")
    } | set(keys)
    for app_id in required:
        definition = registry.get(app_id)
        if "releases" not in definition.public_file_areas:
            raise ValueError(f"Application does not declare public releases: {app_id}")
        roots = settings.get(app_id, {}).get("public_file_roots", {})
        root = roots.get("releases")
        key_hash = keys.get(app_id, "")
        if not root or not isinstance(key_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", key_hash):
            raise ValueError(
                f"Public release storage/key configuration is incomplete: {app_id}; "
                "prepare the build input with tools/configure_public_releases.py"
            )
        target = PurePosixPath(root)
        if any(
            name != "releases"
            and (
                target == PurePosixPath(other)
                or target in PurePosixPath(other).parents
                or PurePosixPath(other) in target.parents
            )
            for name, other in roots.items()
        ):
            raise ValueError(f"Installer storage overlaps other public content: {app_id}")
        mounts = [
            m for m in api.get("volumes", []) if isinstance(m, dict) and m.get("target") == root
        ]
        if len(mounts) != 1 or mounts[0].get("read_only", False):
            raise ValueError(f"A dedicated writable installer mount is required: {app_id}")
        mount = mounts[0]
        if mount.get("type") == "volume":
            if mount.get("source") not in configuration.get("volumes", {}):
                raise ValueError(f"Installer volume is not declared: {app_id}")
        elif mount.get("type") != "bind" or not mount.get("source", "").startswith("/"):
            raise ValueError(f"Installer storage must be persistent: {app_id}")


def configure(configuration, app_id, key_hash):
    if not isinstance(key_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", key_hash):
        raise ValueError("A SHA-256 publishing key hash is required")
    registry = installed_applications()
    if "releases" not in registry.get(app_id).public_file_areas:
        raise ValueError("Application does not declare public releases")
    api = configuration["services"]["api"]
    env = api["environment"]
    settings = json.loads(env.get("APP_DATA_APPLICATIONS", "{}"))
    roots = settings.setdefault(app_id, {}).setdefault("public_file_roots", {})
    store = roots.setdefault("releases", "/data/app-releases")
    target = PurePosixPath(store)
    if any(
        name != "releases"
        and (
            target == PurePosixPath(root)
            or target in PurePosixPath(root).parents
            or PurePosixPath(root) in target.parents
        )
        for name, root in roots.items()
    ):
        raise ValueError("Installer storage must be separate from other public content")
    keys = json.loads(env.get("APP_DATA_RELEASE_PUBLISH_KEY_HASHES", "{}"))
    if app_id in keys and keys[app_id] != key_hash:
        raise ValueError("Existing publishing key differs; do not rotate it implicitly")
    keys[app_id] = key_hash
    env["APP_DATA_RELEASE_PUBLISH_KEY_HASHES"] = json.dumps(keys)
    registry.validate_settings(settings)
    env["APP_DATA_APPLICATIONS"] = json.dumps(settings, ensure_ascii=False)
    mounts = api.setdefault("volumes", [])
    matching = [m for m in mounts if isinstance(m, dict) and m.get("target") == store]
    if matching:
        matching[0]["read_only"] = False
    elif any(isinstance(m, str) and store in m.split(":")[1:2] for m in mounts):
        raise ValueError("Use an explicit long-form installer mount before configuration")
    else:
        volume = "app_release_data_" + app_id
        mounts.append({"type": "volume", "source": volume, "target": store})
        configuration.setdefault("volumes", {}).setdefault(volume, {})
    return store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--publisher-config", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.compose.resolve() == args.output.resolve():
        parser.error("Output must be a new Compose file; existing deployment files are preserved")
    if args.publisher_config.exists():
        publisher = json.loads(args.publisher_config.read_text(encoding="utf-8"))
        key_file = Path(publisher["tokenFile"])
        key = key_file.read_text(encoding="utf-8").strip()
    else:
        key_file = args.publisher_config.with_suffix(".key")
        if key_file.exists():
            parser.error("Publishing key exists without its config; recover it explicitly")
        key = secrets.token_urlsafe(48)
    if not 32 <= len(key) <= 256 or not re.fullmatch(r"[\x21-\x7e]+", key):
        parser.error("Invalid private publishing key")
    configuration = yaml.safe_load(args.compose.read_text(encoding="utf-8-sig"))
    configure(configuration, args.app_id, hashlib.sha256(key.encode()).hexdigest())
    validate_delivery(configuration)
    origin = configuration["services"]["api"]["environment"]["APP_DATA_ISSUER"]
    from urllib.parse import urlsplit

    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        parser.error("Publishing requires an HTTPS service origin")
    if args.publisher_config.exists() and publisher.get("origin", "").rstrip("/") != origin.rstrip(
        "/"
    ):
        parser.error(
            "Existing publisher origin differs from this Compose; no configuration changed"
        )
    publisher = {"origin": origin.rstrip("/"), "tokenFile": str(key_file.resolve())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.publisher_config.parent.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        with key_file.open("x", encoding="utf-8") as output:
            output.write(key)
    if not args.publisher_config.exists():
        with args.publisher_config.open("x", encoding="utf-8") as output:
            json.dump(publisher, output, ensure_ascii=False, indent=2)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(
            "# Build input: requires the new FastAPI image containing migration "
            "0016_release_architecture. Do not deploy with the old image tag.\n"
        )
        yaml.safe_dump(configuration, output, allow_unicode=True, sort_keys=False)
    print(
        "Prepared private Compose and publisher configuration; no upload or deployment performed."
    )


if __name__ == "__main__":
    main()

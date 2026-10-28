"""Prepare an existing private Compose and publisher configuration; never contact NAS."""

import argparse
import hashlib
import json
import secrets
from pathlib import Path

import yaml

from app_data_service.modules.registry import installed_applications


def configure(configuration, app_id, key_hash):
    registry = installed_applications()
    if "releases" not in registry.get(app_id).public_file_areas:
        raise ValueError("Application does not declare public releases")
    api = configuration["services"]["api"]
    env = api["environment"]
    settings = json.loads(env.get("APP_DATA_APPLICATIONS", "{}"))
    roots = settings.setdefault(app_id, {}).setdefault("public_file_roots", {})
    store = roots.setdefault("releases", "/data/app-releases")
    if any(root == store for name, root in roots.items() if name != "releases"):
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
    configuration = yaml.safe_load(args.compose.read_text(encoding="utf-8-sig"))
    configure(configuration, args.app_id, hashlib.sha256(key.encode()).hexdigest())
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
            "# Requires the FastAPI image containing migration 0015 and public releases.\n"
        )
        yaml.safe_dump(configuration, output, allow_unicode=True, sort_keys=False)
    print(
        "Prepared private Compose and publisher configuration; no upload or deployment performed."
    )


if __name__ == "__main__":
    main()

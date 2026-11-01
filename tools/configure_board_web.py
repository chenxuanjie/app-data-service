"""Prepare new private API and Web Compose files; never modify NAS or existing files."""

import argparse
import copy
import json
import re
from pathlib import Path

import yaml

from app_data_service.origins import browser_origin


def configure(api, web, *, origin, previews=(), library_volume=None):
    api, web = copy.deepcopy(api), copy.deepcopy(web)
    environment = api["services"]["api"]["environment"]
    applications = json.loads(environment.get("APP_DATA_APPLICATIONS", "{}"))
    settings = applications.setdefault("board_game_agent", {})
    origin = browser_origin(origin)
    allowed = sorted({browser_origin(v) for v in previews} - {origin})
    if len(allowed) > 16:
        raise ValueError("Too many browser preview origins")
    own = {origin, *allowed}
    for name, other in applications.items():
        if name == "board_game_agent":
            continue
        sites = {other.get("browser_origin"), *other.get("browser_allowed_origins", [])}
        if own.intersection(sites):
            raise ValueError("Browser origins overlap another application")
    settings["browser_origin"] = origin
    settings["browser_allowed_origins"] = allowed
    settings["nginx_library_files"] = library_volume is not None
    frontend = web["services"]["board-game-agent"]
    frontend.setdefault("environment", {})["LIBRARY_ACCELERATION"] = "1" if library_volume else "0"
    web["services"]["web-ai"]["environment"]["WEB_PUBLIC_ORIGIN"] = origin
    if library_volume:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", library_volume):
            raise ValueError("Use the existing Docker library volume name")
        # Match the existing API reader, including restrictive NAS directory modes.
        # 10001:10001 is the API Dockerfile default; no chown/chmod is performed.
        reader = str(api["services"]["api"].get("user", "10001:10001"))
        if not re.fullmatch(r"[1-9][0-9]*:[0-9]+", reader):
            raise ValueError("API library reader must use a non-root numeric UID:GID")
        frontend["user"] = reader
        if "group_add" in api["services"]["api"]:
            frontend["group_add"] = copy.deepcopy(api["services"]["api"]["group_add"])
        if settings.get("public_file_roots", {}).get("library_store") != "/data/board-library":
            raise ValueError("API must use /data/board-library for the shared library store")
        mounts = api["services"]["api"].get("volumes", [])
        if not any(
            isinstance(v, dict)
            and v.get("type") == "volume"
            and v.get("target") == "/data/board-library"
            for v in mounts
        ):
            raise ValueError("An existing API library volume is required")
        web.setdefault("volumes", {})["published_library"] = {
            "external": True,
            "name": library_volume,
        }
        mount = {
            "type": "volume",
            "source": "published_library",
            "target": "/data/board-library",
            "read_only": True,
        }
        frontend_mounts = frontend.setdefault("volumes", [])
        for existing in frontend_mounts:
            if isinstance(existing, dict) and existing.get("target") == mount["target"]:
                if existing != mount:
                    raise ValueError(
                        "Conflicting Web library mount; keep existing files for review"
                    )
                break
            if isinstance(existing, str) and mount["target"] in existing.split(":"):
                raise ValueError("Conflicting Web library mount; use the explicit read-only mount")
        else:
            frontend_mounts.append(mount)
    environment["APP_DATA_APPLICATIONS"] = json.dumps(applications, ensure_ascii=False)
    return api, web


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-compose", type=Path, required=True)
    parser.add_argument("--web-compose", type=Path, required=True)
    parser.add_argument("--api-output", type=Path, required=True)
    parser.add_argument("--web-output", type=Path, required=True)
    parser.add_argument("--browser-origin", default="https://cznas.pp.ua:8084")
    parser.add_argument("--preview-origin", action="append", default=[])
    parser.add_argument(
        "--library-volume",
        help="Actual existing Docker volume name; enables read-only Nginx delivery",
    )
    args = parser.parse_args()
    outputs = [args.api_output.resolve(), args.web_output.resolve()]
    inputs = {args.api_compose.resolve(), args.web_compose.resolve()}
    if len(set(outputs)) != 2 or any(p.exists() or p in inputs for p in outputs):
        parser.error("Keep existing files; choose two new output paths")
    api, web = configure(
        yaml.safe_load(args.api_compose.read_text(encoding="utf-8-sig")),
        yaml.safe_load(args.web_compose.read_text(encoding="utf-8-sig")),
        origin=args.browser_origin,
        previews=args.preview_origin,
        library_volume=args.library_volume,
    )
    for path, document in zip(outputs, (api, web), strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            yaml.safe_dump(document, output, allow_unicode=True, sort_keys=False)
        path.chmod(0o600)
    print(
        "Prepared API and Web Compose; deployment identities and secrets retained. NAS unchanged."
    )


if __name__ == "__main__":
    main()

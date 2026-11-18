"""Generate a private API Compose input with XY-CAR's browser origin."""

import argparse
import json
from pathlib import Path

import yaml

from app_data_service.modules.registry import installed_applications

APP_ID = "xy_car_app"
PRODUCTION_ORIGIN = "https://cznas.pp.ua:8090"
PREVIEW_ORIGINS = (
    "http://127.0.0.1:8089",
    "http://localhost:8089",
    "https://127.0.0.1:8090",
    "https://localhost:8090",
)


def configure(compose, *, origin=PRODUCTION_ORIGIN, previews=PREVIEW_ORIGINS):
    environment = compose["services"]["api"]["environment"]
    applications = json.loads(environment.get("APP_DATA_APPLICATIONS", "{}"))
    xy_car = applications.setdefault(APP_ID, {})
    xy_car["browser_origin"] = origin
    xy_car["browser_allowed_origins"] = [
        *dict.fromkeys([*xy_car.get("browser_allowed_origins", ()), *previews])
    ]
    checked = installed_applications().validate_settings(applications)
    # Validate normalized origins across all apps but change only the XY-CAR entry.
    xy_car["browser_origin"] = checked[APP_ID]["browser_origin"]
    xy_car["browser_allowed_origins"] = checked[APP_ID]["browser_allowed_origins"]
    environment["APP_DATA_APPLICATIONS"] = json.dumps(applications, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser-origin", default=PRODUCTION_ORIGIN)
    parser.add_argument("--preview-origin", action="append")
    arguments = parser.parse_args()
    if arguments.output.exists() or arguments.compose.resolve() == arguments.output.resolve():
        parser.error("Output must be a new file")
    try:
        compose = yaml.safe_load(arguments.compose.read_text(encoding="utf-8-sig"))
        configure(
            compose,
            origin=arguments.browser_origin,
            previews=arguments.preview_origin or PREVIEW_ORIGINS,
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        with arguments.output.open("x", encoding="utf-8") as output:
            yaml.safe_dump(compose, output, allow_unicode=True, sort_keys=False)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        parser.error("Unable to prepare XY-CAR origin configuration; check input and conflicts")
    print("Prepared XY-CAR browser origins in a private API Compose input.")


if __name__ == "__main__":
    main()

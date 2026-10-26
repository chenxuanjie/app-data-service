"""Render the installed modules' PowerSync streams; writes only the requested local file."""

import argparse
from pathlib import Path

import yaml

from app_data_service.modules.registry import installed_applications


def render(registry=None):
    registry = registry or installed_applications()
    return yaml.safe_dump(registry.sync_config(), sort_keys=False, allow_unicode=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(), encoding="utf-8")


if __name__ == "__main__":
    main()

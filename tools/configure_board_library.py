"""Prepare private Compose for a published library, without imports or NAS writes."""

import argparse
from pathlib import Path

import yaml
from render_stack import configure_board_library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.compose.resolve():
        parser.exit(1, "Existing Compose retained; use a new output path.\n")
    config = yaml.safe_load(args.compose.read_text(encoding="utf-8-sig"))
    configure_board_library(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
    args.output.chmod(0o600)
    print("Prepared private Compose; import hook removed, published storage and secrets retained.")


if __name__ == "__main__":
    main()

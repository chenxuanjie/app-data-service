"""Prepare private Compose for startup import, without rotating secrets or writing to NAS."""

import argparse
from pathlib import Path

import yaml
from render_stack import configure_board_library


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--source", required=True, help="NAS physical path, not a Windows drive")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.resolve() == args.compose.resolve():
        parser.exit(1, "Existing Compose retained; use a new output path.\n")
    config = yaml.safe_load(args.compose.read_text(encoding="utf-8-sig"))
    configure_board_library(config, args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
    args.output.chmod(0o600)
    print("Prepared private Compose; no passwords printed, no NAS changes, new API image required.")


if __name__ == "__main__":
    main()

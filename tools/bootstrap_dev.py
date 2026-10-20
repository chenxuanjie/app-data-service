"""Generate local credentials without changing existing ones or printing secrets."""

import json
import secrets
from pathlib import Path

from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    config_path = local / "dev-config.json"
    env_path = local / "dev.env"
    if config_path.exists() or env_path.exists():
        raise SystemExit("Existing credentials retained; refusing to overwrite .local/dev files")
    passwords = {
        role: secrets.token_urlsafe(32) for role in ("ADMIN", "MIGRATOR", "RUNTIME", "REPLICATION")
    }
    env_text = "".join(f"APP_{role}_PASSWORD={password}\n" for role, password in passwords.items())
    urls = {
        role.lower(): URL.create(
            "postgresql+psycopg",
            username=f"app_{role.lower()}",
            password=password,
            host="127.0.0.1",
            port=55432,
            database="app_data",
        ).render_as_string(hide_password=False)
        for role, password in passwords.items()
    }
    # Exclusive create: never rotate an initialized database's passwords implicitly.
    with env_path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(env_text)
    with config_path.open("x", encoding="utf-8", newline="\n") as file:
        json.dump({"profile": "local-development", "urls": urls}, file, indent=2)
        file.write("\n")
    env_path.chmod(0o600)
    config_path.chmod(0o600)
    print(
        "Created ignored .local/dev.env and .local/dev-config.json; credentials were not printed."
    )


if __name__ == "__main__":
    main()

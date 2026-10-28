import base64
import importlib
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy.engine import make_url


def test_public_application_mounts_are_explicit_readonly_and_never_create_host_directories(
    monkeypatch,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    renderer = importlib.import_module("render_stack")
    settings = {"board_game_agent": {"public_file_roots": {"library": "/data/public/board"}}}
    args = ("/volume1/test-postgres", "https://api.test", "https://dsm.test:5001")
    with pytest.raises(ValueError, match="explicit absolute host mount"):
        renderer.stack(*args, application_settings=settings)
    config = renderer.stack(
        *args,
        application_settings=settings,
        public_mounts={"/data/public/board": "/test/published"},
    )
    assert config["services"]["api"]["volumes"] == [
        {
            "type": "bind",
            "source": "/test/published",
            "target": "/data/public/board",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]


def test_development_stream_config_matches_installed_modules():
    from app_data_service.modules.registry import installed_applications

    path = Path(__file__).resolve().parents[1] / "deploy/powersync/sync-config.yaml"
    assert (
        yaml.safe_load(path.read_text(encoding="utf-8")) == installed_applications().sync_config()
    )


def test_published_library_keeps_three_services_without_startup_import(monkeypatch):
    from copy import deepcopy

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    renderer = importlib.import_module("render_stack")
    config = renderer.stack("/volume1/test/data", "https://api.test", "https://dsm.test:5001")
    original = deepcopy(config)
    renderer.configure_board_library(config)
    assert set(config["services"]) == {"api", "postgres", "powersync"}
    assert config["services"]["postgres"] == original["services"]["postgres"]
    assert config["services"]["powersync"] == original["services"]["powersync"]
    api = config["services"]["api"]
    for key, value in original["services"]["api"]["environment"].items():
        if key != "APP_DATA_APPLICATIONS":
            assert api["environment"][key] == value
    assert api["volumes"] == [
        {"type": "volume", "source": "board_library_data", "target": "/data/board-library"}
    ]
    assert config["volumes"]["board_library_data"] == {}
    assert "start_period" not in api["healthcheck"]
    startup = api["command"][2]
    assert "board-auto-import-library" not in startup
    assert "APP_DATA_BOARD_LIBRARY_SOURCE" not in api["environment"]
    assert startup.index("register-applications") < startup.index("unset APP_DATA_MIGRATION_URL")
    assert "cpus" not in api and "user" not in api
    first = deepcopy(config)
    renderer.configure_board_library(config)
    assert config == first
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "chown 10001:10001 /data/board-library" in dockerfile
    assert "USER 10001:10001" in dockerfile


@pytest.mark.parametrize("short_mount", [False, True])
def test_completed_import_cleanup_preserves_existing_store_and_other_app_settings(
    monkeypatch, short_mount
):
    from copy import deepcopy

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    renderer = importlib.import_module("render_stack")
    config = renderer.stack("/volume1/test/data", "https://api.test", "https://dsm.test:5001")
    api = config["services"]["api"]
    roots = {"board_game_agent": {"public_file_roots": {"library_store": "/existing/store"}}}
    api["environment"].update(
        {
            "APP_DATA_APPLICATIONS": json.dumps(roots),
            "APP_DATA_BOARD_LIBRARY_SOURCE": "/import/board-library",
            "APP_DATA_BOARD_LIBRARY_STORE": "/existing/store",
            "APP_DATA_BOARD_LIBRARY_IMPORT_ID": "initial",
            "APP_DATA_BOARD_LIBRARY_SQL_MAX_BYTES": "4194304",
        }
    )
    api["volumes"] = [
        "/nas/source:/import/board-library:ro"
        if short_mount
        else {"type": "bind", "source": "/nas/source", "target": "/import/board-library"},
        "actual_existing_volume:/existing/store",
        {"type": "bind", "source": "/other/app", "target": "/other", "read_only": True},
    ]
    api["healthcheck"]["start_period"] = "10m"
    api["group_add"] = ["100"]
    before = deepcopy(config)
    renderer.configure_board_library(config)
    assert api["volumes"] == before["services"]["api"]["volumes"][1:]
    assert api["group_add"] == ["100"]
    assert api["environment"] == {
        k: v
        for k, v in before["services"]["api"]["environment"].items()
        if not k.startswith("APP_DATA_BOARD_LIBRARY_")
    }
    assert "start_period" not in api["healthcheck"]
    assert config["services"]["postgres"] == before["services"]["postgres"]
    assert config["services"]["powersync"] == before["services"]["powersync"]
    assert config.get("volumes") == before.get("volumes")
    after = deepcopy(config)
    renderer.remove_board_library_startup(config)
    assert config == after


def test_full_synology_stack_has_private_database_and_stable_identity(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    renderer = importlib.import_module("render_stack")
    config = renderer.stack(
        "/volume2/friend/apps/app_data_service_test/postgres",
        "https://data.example.test:18280",
        "https://nas.example.test:5001",
        dsm_identity_origin="https://old-nas.example.test:5001",
        dsm_lan_ip="192.168.1.55",
    )
    assert set(config["services"]) == {"postgres", "api", "powersync"}
    assert "${" not in yaml.safe_dump(config)
    for name, service in config["services"].items():
        assert "build" not in service and "env_file" not in service
        assert service["platform"] == "linux/amd64"
        # This user's NAS kernel rejects CFS quotas at container creation.
        assert not {"cpus", "cpu_quota", "cpu_period"} & service.keys()
        assert "mem_limit" in service
        if name != "api":
            assert "ports" not in service
    api = config["services"]["api"]
    assert api["environment"]["APP_DATA_SYNC_ENDPOINT"].endswith("/powersync/")
    assert api["environment"]["APP_DATA_DSM_IDENTITY_ORIGIN"] == "https://old-nas.example.test:5001"
    assert api["extra_hosts"] == ["nas.example.test:192.168.1.55"]
    assert make_url(api["environment"]["APP_DATA_DATABASE_URL"]).username == "app_runtime"
    assert api["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert make_url(api["environment"]["APP_DATA_MIGRATION_URL"]).username == "app_migrator"
    assert api["command"][:2] == ["/bin/sh", "-ec"]
    startup = api["command"][2]
    assert startup.index("app-data migrate") < startup.index("unset APP_DATA_MIGRATION_URL")
    assert startup.index("unset APP_DATA_MIGRATION_URL") < startup.index("exec python")
    decoded = yaml.safe_load(
        base64.b64decode(config["services"]["powersync"]["environment"]["POWERSYNC_CONFIG_B64"])
    )
    assert decoded["storage"]["type"] == "postgresql"
    assert decoded["sync_config"]["content"].startswith("config:")
    assert decoded["client_auth"]["jwks_uri"] == "http://api:8000/.well-known/jwks.json"


def test_optional_account_file_roots_and_browser_gateway_are_explicit(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    renderer = importlib.import_module("render_stack")
    config = renderer.stack(
        "/volume1/test/data",
        "https://data.test",
        "https://nas.test:5001",
        application_settings={
            "zheri": {
                "browser_origin": "https://web.test",
                "file_roots": {"updates": "/apps/zheri/updates", "resources": "/home/Apps/zheri"},
            }
        },
    )
    env = config["services"]["api"]["environment"]
    settings = json.loads(env["APP_DATA_APPLICATIONS"])["zheri"]
    assert settings["browser_origin"] == "https://web.test"
    assert settings["file_roots"] == {
        "updates": "/apps/zheri/updates",
        "resources": "/home/Apps/zheri",
    }
    assert set(config["services"]) == {"api", "postgres", "powersync"}


def test_rules_update_preserves_all_private_settings(monkeypatch):
    from copy import deepcopy

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    renderer = importlib.import_module("render_stack")
    bundler = importlib.import_module("build_docker_bundle")
    config = renderer.stack(
        "/volume1/test/data", "https://data.example.test", "https://nas.example.test:5001"
    )
    before = deepcopy(config)
    environment = config["services"]["powersync"]["environment"]
    old_rules = yaml.safe_load(base64.b64decode(environment["POWERSYNC_CONFIG_B64"]))
    expected = deepcopy(old_rules)
    expected["sync_config"]["content"] = "new rules"
    bundler.update_sync_rules(config, "new rules")
    assert yaml.safe_load(base64.b64decode(environment["POWERSYNC_CONFIG_B64"])) == expected
    environment["POWERSYNC_CONFIG_B64"] = before["services"]["powersync"]["environment"][
        "POWERSYNC_CONFIG_B64"
    ]
    assert config == before

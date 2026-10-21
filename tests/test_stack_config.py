import base64
import importlib
from pathlib import Path

import yaml
from sqlalchemy.engine import make_url


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

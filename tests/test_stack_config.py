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
    assert set(config["services"]) == {"postgres", "migrate", "api", "powersync"}
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
    assert api["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    decoded = yaml.safe_load(
        base64.b64decode(config["services"]["powersync"]["environment"]["POWERSYNC_CONFIG_B64"])
    )
    assert decoded["storage"]["type"] == "postgresql"
    assert decoded["sync_config"]["content"].startswith("config:")
    assert decoded["client_auth"]["jwks_uri"] == "http://api:8000/.well-known/jwks.json"

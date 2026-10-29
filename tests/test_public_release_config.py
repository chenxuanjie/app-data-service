import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

spec = importlib.util.spec_from_file_location(
    "public_release_config", Path(__file__).parents[1] / "tools/configure_public_releases.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_adds_installer_storage_and_a_scoped_machine_key():
    config = {
        "services": {
            "api": {
                "image": "existing",
                "environment": {
                    "APP_DATA_ISSUER": "https://api.test",
                    "APP_DATA_DATABASE_URL": "preserved",
                    "APP_DATA_APPLICATIONS": json.dumps(
                        {"zheri": {"file_roots": {"updates": "/old"}}}
                    ),
                },
            }
        }
    }
    before = copy.deepcopy(config)
    assert module.configure(config, "board_game_agent", "a" * 64) == "/data/app-releases"
    api = config["services"]["api"]
    assert api["image"] == before["services"]["api"]["image"]
    assert api["environment"]["APP_DATA_DATABASE_URL"] == "preserved"
    assert json.loads(api["environment"]["APP_DATA_APPLICATIONS"])["zheri"] == {
        "file_roots": {"updates": "/old"}
    }
    assert len(api["volumes"]) == 1
    module.configure(config, "board_game_agent", "a" * 64)
    assert len(api["volumes"]) == 1
    with pytest.raises(ValueError, match="rotate"):
        module.configure(config, "board_game_agent", "b" * 64)
    with pytest.raises(ValueError, match="declare"):
        module.configure(before, "zheri", "a" * 64)


def test_delivery_rejects_missing_release_settings_or_persistence_before_build(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "tools"))
    import build_docker_bundle as bundler

    config = {
        "services": {
            "postgres": {},
            "powersync": {},
            "api": {
                "environment": {
                    "APP_DATA_APPLICATIONS": json.dumps(
                        {
                            "board_game_agent": {
                                "public_file_roots": {"library_store": "/data/board-library"}
                            }
                        }
                    ),
                }
            },
        }
    }
    before = copy.deepcopy(config)
    with pytest.raises(ValueError, match="incomplete"):
        module.validate_delivery(config)
    path = tmp_path / "compose.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    class NoBuild:
        def run(self, *args, **kwargs):
            pytest.fail("Incomplete Compose reached Docker/network build work")

    with pytest.raises(ValueError, match="incomplete"):
        bundler.build(SimpleNamespace(compose=path, docker="native"), log=NoBuild())
    assert config == before
    module.configure(config, "board_game_agent", "a" * 64)
    module.validate_delivery(config)
    configured = copy.deepcopy(config)
    config["services"]["api"]["volumes"][0]["read_only"] = True
    with pytest.raises(ValueError, match="writable"):
        module.validate_delivery(config)
    config = copy.deepcopy(configured)
    config["volumes"].clear()
    with pytest.raises(ValueError, match="not declared"):
        module.validate_delivery(config)
    config = copy.deepcopy(configured)
    settings = json.loads(config["services"]["api"]["environment"]["APP_DATA_APPLICATIONS"])
    settings["board_game_agent"]["public_file_roots"]["releases"] = "/data/board-library/installers"
    config["services"]["api"]["environment"]["APP_DATA_APPLICATIONS"] = json.dumps(settings)
    with pytest.raises(ValueError, match="overlaps"):
        module.validate_delivery(config)

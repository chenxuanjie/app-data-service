import copy
import importlib.util
import json
from pathlib import Path

import pytest

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

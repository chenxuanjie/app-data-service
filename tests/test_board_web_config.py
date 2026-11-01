import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "configure_board_web", Path(__file__).resolve().parents[1] / "tools/configure_board_web.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
configure = module.configure


def fixture():
    settings = {
        "zheri": {
            "browser_origin": "https://zheri.test",
            "file_roots": {"updates": "/private/updates"},
        },
        "board_game_agent": {"public_file_roots": {"library_store": "/data/board-library"}},
    }
    api = {
        "name": "kept",
        "services": {
            "api": {
                "image": "kept-api",
                "user": "1028:100",
                "group_add": ["10001"],
                "environment": {
                    "APP_DATA_APPLICATIONS": json.dumps(settings),
                    "APP_DATA_SESSION_ENCRYPTION_KEY": "fixture-only-kept",
                },
                "volumes": [
                    {"type": "volume", "source": "library", "target": "/data/board-library"}
                ],
            },
            "postgres": {"image": "kept-postgres"},
        },
        "volumes": {"library": {}},
    }
    web = {
        "services": {
            "board-game-agent": {"ports": ["8083:8080"], "environment": {}},
            "web-ai": {"environment": {}},
        }
    }
    return api, web


def test_web_preparation_preserves_other_apps_secrets_versions_and_originals():
    api, web = fixture()
    before = copy.deepcopy((api, web))
    prepared, frontend = configure(
        api,
        web,
        origin="https://games.test",
        previews=["https://localhost:8084"],
        library_volume="existing_project_board_library_data",
    )
    assert (api, web) == before
    environment = prepared["services"]["api"]["environment"]
    applications = json.loads(environment["APP_DATA_APPLICATIONS"])
    assert (
        applications["zheri"]
        == json.loads(api["services"]["api"]["environment"]["APP_DATA_APPLICATIONS"])["zheri"]
    )
    assert environment["APP_DATA_SESSION_ENCRYPTION_KEY"] == "fixture-only-kept"
    assert prepared["services"]["api"]["image"] == "kept-api"
    assert frontend["volumes"]["published_library"]["external"] is True
    assert frontend["services"]["board-game-agent"]["volumes"][0]["read_only"] is True
    assert frontend["services"]["board-game-agent"]["user"] == "1028:100"
    assert frontend["services"]["board-game-agent"]["group_add"] == ["10001"]
    assert applications["board_game_agent"]["browser_allowed_origins"] == ["https://localhost:8084"]
    repeated, repeated_web = configure(
        prepared,
        frontend,
        origin="https://games.test",
        previews=["https://localhost:8084"],
        library_volume="existing_project_board_library_data",
    )
    assert repeated == prepared and repeated_web == frontend


def test_web_preparation_rejects_cross_app_origins_and_non_volume_mounts():
    api, web = fixture()
    with pytest.raises(ValueError, match="overlap"):
        configure(api, web, origin="https://games.test", previews=["https://zheri.test"])
    with pytest.raises(ValueError, match="volume name"):
        configure(api, web, origin="https://games.test", library_volume="/volume1/private")
    api["services"]["api"]["volumes"] = []
    with pytest.raises(ValueError, match="existing API"):
        configure(api, web, origin="https://games.test", library_volume="existing")

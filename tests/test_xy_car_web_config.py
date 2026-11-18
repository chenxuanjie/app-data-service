import json

import pytest

from tools.configure_xy_car_web import PREVIEW_ORIGINS, PRODUCTION_ORIGIN, configure


def compose_with_board_origin(origin):
    environment = {
        "APP_DATA_APPLICATIONS": json.dumps({"board_game_agent": {"browser_origin": origin}}),
        "APP_DATA_TRUSTED_PROXIES": "127.0.0.1/32",
    }
    return {"services": {"api": {"environment": environment, "volumes": ["data:/data"]}}}


def test_xy_car_origin_config_is_separate_and_repeatable():
    compose = compose_with_board_origin("https://cznas.pp.ua:8084")
    configure(compose)
    configure(compose)
    api = compose["services"]["api"]
    data = json.loads(api["environment"]["APP_DATA_APPLICATIONS"])
    assert data["xy_car_app"]["browser_origin"] == PRODUCTION_ORIGIN
    assert set(data["xy_car_app"]["browser_allowed_origins"]) == set(PREVIEW_ORIGINS)
    assert data["board_game_agent"] == {"browser_origin": "https://cznas.pp.ua:8084"}
    assert api["environment"]["APP_DATA_TRUSTED_PROXIES"] == "127.0.0.1/32"
    assert api["volumes"] == ["data:/data"]


def test_xy_car_origin_config_rejects_an_existing_application_origin():
    compose = compose_with_board_origin(PRODUCTION_ORIGIN)
    with pytest.raises(ValueError, match="own browser origin"):
        configure(compose)

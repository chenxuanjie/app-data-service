"""Deployment trust is explicit, validated and preserved by unrelated edits."""

import copy
import importlib
from pathlib import Path

import pytest
from test_board_web_config import fixture


def tools(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("render_stack"), importlib.import_module("configure_board_web")


def test_stack_keeps_uvicorn_header_rewriting_off_and_requires_explicit_proxy_trust(monkeypatch):
    renderer, _ = tools(monkeypatch)
    args = ("/volume1/test/data", "https://api.test", "https://dsm.test:5001")
    service = renderer.stack(*args)["services"]["api"]
    assert service["environment"]["APP_DATA_TRUSTED_PROXIES"] == ""
    assert "--no-proxy-headers" in service["command"][-1]
    service = renderer.stack(*args, trusted_proxies=["10.0.0.2", "2001:db8::2"])["services"]["api"]
    assert service["environment"]["APP_DATA_TRUSTED_PROXIES"] == "10.0.0.2/32,2001:db8::2/128"
    with pytest.raises(ValueError):
        renderer.stack(*args, trusted_proxies=["0.0.0.0/0"])


def test_configure_web_preserves_existing_trust_or_updates_both_hops_explicitly(monkeypatch):
    _, config = tools(monkeypatch)
    api, web = fixture()
    api["services"]["api"]["environment"]["APP_DATA_TRUSTED_PROXIES"] = "10.0.0.2/32"
    web["services"]["board-game-agent"].setdefault("environment", {})["BGA_TRUSTED_PROXIES"] = (
        "10.0.0.1/32"
    )
    before = copy.deepcopy((api, web))
    kept_api, kept_web = config.configure(api, web, origin="https://games.test")
    assert kept_api["services"]["api"]["environment"]["APP_DATA_TRUSTED_PROXIES"] == "10.0.0.2/32"
    assert (
        kept_web["services"]["board-game-agent"]["environment"]["BGA_TRUSTED_PROXIES"]
        == "10.0.0.1/32"
    )
    changed_api, changed_web = config.configure(
        api,
        web,
        origin="https://games.test",
        trusted_api_proxies=["10.1.0.2"],
        trusted_web_proxies=["10.1.0.1"],
    )
    assert (
        changed_api["services"]["api"]["environment"]["APP_DATA_TRUSTED_PROXIES"] == "10.1.0.2/32"
    )
    assert (
        changed_web["services"]["board-game-agent"]["environment"]["BGA_TRUSTED_PROXIES"]
        == "10.1.0.1/32"
    )
    assert (api, web) == before
    with pytest.raises(ValueError):
        config.configure(api, web, origin="https://games.test", trusted_api_proxies=["*"])

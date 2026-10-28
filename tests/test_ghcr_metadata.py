import importlib

import pytest


@pytest.fixture
def metadata(monkeypatch):
    monkeypatch.syspath_prepend("tools")
    return importlib.import_module("ghcr_metadata").image_metadata


def test_feature_build_has_branch_and_commit_without_stable_tags(metadata):
    image, tags = metadata("Owner/API", "a" * 40, "refs/heads/feature/catalog", "0.5.0")
    assert image == "ghcr.io/owner/api"
    assert tags == [f"{image}:sha-{'a' * 40}", f"{image}:branch-feature-catalog"]


def test_release_tag_must_match_project_version(metadata):
    with pytest.raises(ValueError, match="must match"):
        metadata("owner/api", "a" * 40, "refs/tags/v0.4.0", "0.5.0")
    image, tags = metadata("owner/api", "a" * 40, "refs/tags/v0.5.0", "0.5.0")
    assert f"{image}:0.5.0" in tags
    assert f"{image}:latest" in tags


def test_pull_request_does_not_get_branch_or_stable_tag(metadata):
    image, tags = metadata("owner/api", "a" * 40, "refs/pull/12/merge", "0.5.0")
    assert tags == [f"{image}:sha-{'a' * 40}"]


def test_main_gets_latest_but_does_not_overwrite_version_tag(metadata):
    image, tags = metadata("owner/api", "a" * 40, "refs/heads/main", "0.5.0")
    assert f"{image}:latest" in tags
    assert f"{image}:0.5.0" not in tags

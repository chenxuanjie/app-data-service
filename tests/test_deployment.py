import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "render_synology", ROOT / "tools" / "render_synology.py"
)
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


def test_synology_compose_is_self_contained_and_database_is_private():
    passwords = {role: "test-secret" for role in ("ADMIN", "MIGRATOR", "RUNTIME", "REPLICATION")}
    configuration = render.compose("/volume1/docker/app-data-service/postgres", passwords)
    text = yaml.safe_dump(configuration)
    service = configuration["services"]["postgres"]
    assert "ports" not in service and "build" not in service and "env_file" not in service
    assert "${" not in text
    assert "CREATE ROLE app_runtime" in service["environment"]["APP_BOOTSTRAP_SQL"]
    assert service["volumes"][0]["bind"]["create_host_path"] is False
    assert service["entrypoint"] == ["/bin/bash", "-ec"]


@pytest.mark.parametrize(
    "directory",
    ["/", "/volume1", "../postgres", "/volume1/../etc", "/volume1/$HOME/data", "C:/data"],
)
def test_renderer_rejects_ambiguous_or_expanding_mounts(directory):
    with pytest.raises(ValueError):
        render.compose(directory, {})

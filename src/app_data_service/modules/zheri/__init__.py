"""Zheri module definition. Domain semantics never enter shared login/session code."""

from app_data_service.applications import ApplicationDefinition

from .cli import register_commands
from .entities import ENTITY_MODELS
from .routes import build_router

STREAMS = {
    f"private_{table}": {
        "auto_subscribe": True,
        "query": (
            f"SELECT {table}.* FROM zheri.{table}\n"
            "WHERE deleted_at IS NULL AND dataset_id IN (\n"
            "  SELECT id FROM core.datasets WHERE app_id = auth.parameter('app_id') "
            "AND app_id = 'zheri' AND id IN (\n"
            "    SELECT dataset_id FROM core.dataset_members WHERE user_id = auth.user_id()\n"
            "  )\n)\n"
        ),
    }
    for table in ENTITY_MODELS
}

definition = ApplicationDefinition(
    id="zheri",
    name="折日",
    schema="zheri",
    personal_space_name="个人账本",
    personal_id_prefix="personal-",
    file_areas=("updates", "resources"),
    capabilities={
        "sync_protocols": [2],
        "entities": list(ENTITY_MODELS),
        "max_mutations": 100,
        "record_merge": "three_way_v1",
        "automatic_preferences": {
            "allocation_baseline": "latest_edit",
            "allocation_order": "latest_edit",
        },
    },
    sync_streams=STREAMS,
    replication_tables=tuple(f"zheri.{table}" for table in ENTITY_MODELS),
    cli_factory=register_commands,
    router_factory=build_router,
)

"""Board-game domain registration; platform authentication stays application-independent."""

from app_data_service.applications import ApplicationDefinition

from .cli import register_commands
from .models import PERSONAL
from .routes import build_router

definition = ApplicationDefinition(
    id="board_game_agent",
    name="桌游助手",
    schema="board_game_agent",
    public_file_areas=("library_store", "releases"),
    capabilities={
        "sync_protocols": [1],
        "entities": list(PERSONAL),
        "catalog_protocols": [1],
        "public_releases": 1,
        "community": True,
    },
    router_factory=build_router,
    cli_factory=register_commands,
    replication_tables=tuple(
        f"board_game_agent.{table}" for table in (*PERSONAL, "public_changes")
    ),
    sync_streams={
        f"board_private_{table}": {
            "auto_subscribe": True,
            "query": f"SELECT {table}.* FROM board_game_agent.{table} "
            "WHERE auth.parameter('sync_scope') IS NULL AND deleted_at IS NULL AND dataset_id IN ("
            "SELECT id FROM core.datasets WHERE app_id=auth.parameter('app_id') "
            "AND app_id='board_game_agent' AND id IN ("
            "SELECT dataset_id FROM core.dataset_members WHERE user_id=auth.user_id()))",
        }
        for table in PERSONAL
    }
    | {
        "board_public_changes": {
            "auto_subscribe": True,
            "query": "SELECT * FROM board_game_agent.public_changes "
            "WHERE auth.parameter('app_id')='board_game_agent' "
            "AND auth.parameter('sync_scope')='public'",
        }
    },
)

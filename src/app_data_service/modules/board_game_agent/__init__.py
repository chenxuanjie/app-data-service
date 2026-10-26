"""Board-game domain registration; platform authentication stays application-independent."""

from app_data_service.applications import ApplicationDefinition

from .cli import register_commands
from .models import PERSONAL
from .routes import build_router

definition = ApplicationDefinition(
    id="board_game_agent",
    name="桌游助手",
    schema="board_game_agent",
    public_file_areas=("library",),
    capabilities={
        "sync_protocols": [1],
        "entities": list(PERSONAL),
        "public_library": True,
        "community": True,
    },
    router_factory=build_router,
    cli_factory=register_commands,
    replication_tables=tuple(f"board_game_agent.{table}" for table in PERSONAL),
    sync_streams={
        f"board_private_{table}": {
            "auto_subscribe": True,
            "query": f"SELECT {table}.* FROM board_game_agent.{table} "
            "WHERE deleted_at IS NULL AND dataset_id IN ("
            "SELECT id FROM core.datasets WHERE app_id=auth.parameter('app_id') "
            "AND app_id='board_game_agent' AND id IN ("
            "SELECT dataset_id FROM core.dataset_members WHERE user_id=auth.user_id()))",
        }
        for table in PERSONAL
    },
)

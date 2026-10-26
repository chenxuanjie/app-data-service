"""Application registration; board-game business resources are a later stage."""

from app_data_service.applications import ApplicationDefinition

definition = ApplicationDefinition(
    id="board_game_agent",
    name="桌游助手",
    schema="board_game_agent",
    capabilities={"sync_protocols": [], "entities": []},
)

"""Explicit composition root. Adding an application does not edit authentication code."""

from app_data_service.applications import ApplicationRegistry

from .board_game_agent import definition as board_game_agent
from .zheri import definition as zheri


def installed_applications():
    return ApplicationRegistry((zheri, board_game_agent))

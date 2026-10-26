"""One board-game protocol for public resources, private sync and community."""

import sqlalchemy as sa
from fastapi import HTTPException, Query, Request, Response
from pydantic import ValidationError

from app_data_service.dependencies import (
    Actor,
    Application,
    Credential,
    application_router,
    identity,
)
from app_data_service.store import user_transaction

from . import business
from .library import PublishedLibrary

PREFIX = "/v1/apps/board_game_agent"


def build_router(runtime):
    router = application_router("board_game_agent")
    settings = runtime.application_settings.get("board_game_agent", {})
    library = PublishedLibrary(settings.get("public_file_roots", {}).get("library"))

    @router.get(PREFIX + "/library")
    def publication():
        return library.index()

    @router.api_route(PREFIX + "/library/files/{path:path}", methods=["GET", "HEAD"])
    def published_file(path: str, request: Request):
        return library.file(path, request)

    @router.post(PREFIX + "/sync/upload")
    def sync_upload(body: business.Upload, actor: Actor):
        try:
            with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as c:
                return business.upload(c, actor.user_id, body)
        except ValidationError as error:
            raise HTTPException(422, "board_payload_invalid") from error

    @router.get(PREFIX + "/community/statistics")
    def statistics(
        request: Request, response: Response, app_id: Application, credentials: Credential
    ):
        response.headers["Cache-Control"] = "no-store"
        actor = identity(request, credentials, app_id).user_id if credentials else None
        with user_transaction(runtime.engine, actor or "public-reader", app_id=app_id) as c:
            result = c.scalar(sa.text("SELECT board_game_agent.public_counts()"))
            result["own_favorites"] = []
            result["own_votes"] = []
            if actor:
                from .models import favorites, votes

                result["own_favorites"] = list(
                    c.scalars(
                        sa.select(favorites.c.game_slug).where(favorites.c.deleted_at.is_(None))
                    )
                )
                result["own_votes"] = [
                    dict(r)
                    for r in c.execute(
                        sa.select(votes.c.campaign, votes.c.game_slug).where(
                            votes.c.deleted_at.is_(None)
                        )
                    ).mappings()
                ]
            return result

    @router.get(PREFIX + "/community/comments/{game}")
    def comments(
        game: str,
        request: Request,
        response: Response,
        app_id: Application,
        credentials: Credential,
        cursor: str | None = Query(default=None, max_length=300),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        response.headers["Cache-Control"] = "no-store"
        if not business.re_slug(game):
            raise HTTPException(404, "game_unavailable")
        actor = identity(request, credentials, app_id).user_id if credentials else None
        with user_transaction(runtime.engine, actor or "public-reader", app_id=app_id) as c:
            return business.feed(c, game, actor, cursor=cursor, limit=limit)

    @router.post(PREFIX + "/community/commands")
    def command(body: business.CommunityCommand, actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as c:
            return business.community_command(c, actor.user_id, body)

    return router

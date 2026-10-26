"""Zheri owns its business protocol and its currently shipped API entrypoints."""

from uuid import UUID

import sqlalchemy as sa
from fastapi.responses import JSONResponse

from app_data_service.dependencies import Actor, application_router
from app_data_service.store import user_transaction

from .entities import ENTITY_MODELS
from .models import ExpenseConflict, SyncConflict
from .sync import (
    ReplayError,
    Resolution,
    ScopeError,
    Upload,
    apply_upload,
    current_record,
    resolve_conflict,
    snapshot,
)


def build_router(runtime):
    router = application_router("zheri")
    # These are the shipped Zheri API, owned by this module and using the same core handlers.
    from app_data_service.platform_routes import build_router as platform_router

    platform_router(runtime, router=router, prefix="/v1")

    async def forbidden(_, error):
        return JSONResponse({"detail": str(error)}, 404)

    async def stale(_, error):
        return JSONResponse({"detail": str(error)}, 409)

    runtime.app.add_exception_handler(ScopeError, forbidden)
    runtime.app.add_exception_handler(ReplayError, stale)

    @router.post("/v1/apps/zheri/sync/upload")
    @router.post("/v2/sync/upload")
    def upload_v2(body: Upload, actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            result = apply_upload(connection, actor.user_id, body)
        return result

    @router.get("/v1/apps/zheri/conflicts")
    @router.get("/v2/conflicts")
    def conflicts_v2(actor: Actor, dataset_id: str):
        result = []
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            for entity_type in ENTITY_MODELS:
                table = (ExpenseConflict if entity_type == "expenses" else SyncConflict).__table__
                conditions = [table.c.dataset_id == dataset_id, table.c.resolved_at.is_(None)]
                if entity_type != "expenses":
                    conditions.append(table.c.entity_type == entity_type)
                rows = connection.execute(
                    sa.select(table)
                    .where(*conditions)
                    .order_by(table.c.created_at, table.c.id)
                    .limit(100)
                ).mappings()
                for row in rows:
                    current = current_record(
                        connection,
                        dataset_id,
                        row["record_id"],
                        lock=False,
                        entity_type=entity_type,
                    )
                    result.append(
                        {
                            **dict(row),
                            "entity_type": entity_type,
                            "current_record": snapshot(current),
                            "current_version": current["row_version"] if current else 0,
                        }
                    )
        result.sort(key=lambda row: (row["created_at"], str(row["id"])))
        return {"conflicts": result[:100]}

    @router.post("/v1/apps/zheri/conflicts/{conflict_id}/resolve")
    @router.post("/v2/conflicts/{conflict_id}/resolve")
    def resolve_v2(conflict_id: UUID, body: Resolution, actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            result = resolve_conflict(connection, conflict_id, body, entity_type=body.entity_type)
        return result

    return router

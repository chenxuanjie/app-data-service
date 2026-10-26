"""A third, typed application installed only into isolated test databases."""

from uuid import UUID

import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app_data_service.applications import ApplicationDefinition
from app_data_service.dependencies import Actor, application_router
from app_data_service.models import Dataset
from app_data_service.registration import register_application
from app_data_service.store import user_transaction


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    dataset_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=4000)


def build_router(runtime):
    router = application_router("reading_notes")

    @router.get("/v1/apps/reading_notes/about")
    def about():
        return {"app_id": "reading_notes"}

    @router.get("/v1/apps/reading_notes/notes")
    def notes(actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as c:
            return {
                "notes": [
                    dict(row)
                    for row in c.execute(
                        sa.text(
                            "SELECT id,dataset_id,title,body FROM reading_notes.notes ORDER BY id"
                        )
                    ).mappings()
                ]
            }

    @router.post("/v1/apps/reading_notes/notes")
    def save(note: Note, actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as c:
            if c.scalar(sa.select(Dataset.id).where(Dataset.id == note.dataset_id)) is None:
                raise HTTPException(404, "Dataset unavailable")
            c.execute(
                sa.text(
                    "INSERT INTO reading_notes.notes(id,dataset_id,title,body) "
                    "VALUES(:id,:dataset_id,:title,:body)"
                ),
                note.model_dump(),
            )
        return {"id": str(note.id), "saved": True}

    return router


definition = ApplicationDefinition(
    id="reading_notes",
    name="阅读笔记测试",
    schema="reading_notes",
    personal_space_name="个人笔记",
    file_areas=("attachments", "updates"),
    capabilities={"entities": ["notes"], "sync_protocols": []},
    sync_streams={
        "reading_notes_private": {
            "auto_subscribe": True,
            "query": "SELECT notes.* FROM reading_notes.notes WHERE dataset_id IN ("
            "SELECT id FROM core.datasets WHERE app_id = auth.parameter('app_id') "
            "AND app_id = 'reading_notes' AND id IN (SELECT dataset_id "
            "FROM core.dataset_members WHERE user_id = auth.user_id()))",
        }
    },
    router_factory=build_router,
    replication_tables=("reading_notes.notes",),
)


def install(connection):
    # Only this test module owns this migration. Authentication creates no table or grant.
    connection.exec_driver_sql("""
        CREATE SCHEMA reading_notes;
        REVOKE ALL ON SCHEMA reading_notes FROM PUBLIC;
        CREATE TABLE reading_notes.notes(
          id UUID PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
          title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
          body TEXT NOT NULL CHECK(length(body)<=4000)
        );
        ALTER TABLE reading_notes.notes ENABLE ROW LEVEL SECURITY;
        CREATE POLICY notes_private ON reading_notes.notes TO app_runtime
          USING(core.current_app_id()='reading_notes' AND core.can_access_dataset(dataset_id))
          WITH CHECK(core.current_app_id()='reading_notes' AND core.can_access_dataset(dataset_id));
        GRANT USAGE ON SCHEMA reading_notes TO app_runtime,app_replication;
        GRANT SELECT,INSERT ON reading_notes.notes TO app_runtime;
        GRANT SELECT ON reading_notes.notes TO app_replication;
        ALTER PUBLICATION powersync ADD TABLE reading_notes.notes;
    """)
    register_application(connection, definition)

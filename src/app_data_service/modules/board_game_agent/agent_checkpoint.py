"""Dedicated PostgreSQL storage for the board Agent and its thread registry."""

from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection, Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

CONNECTION = {
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
    "connect_timeout": 5,
    "options": "-c search_path=public -c statement_timeout=10000 -c lock_timeout=2000",
}
SERVICE_LOCK = 1834910327
TABLES = ("agent_threads", "checkpoints", "checkpoint_blobs", "checkpoint_writes")


def setup_checkpoint_database(dsn, role):
    with Connection.connect(dsn, **CONNECTION) as connection:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (SERVICE_LOCK,)
        ).fetchone()
        if not row["locked"]:
            raise ValueError("agent_checkpoint_database_busy")
        if connection.execute("SELECT to_regclass('core.apps') IS NOT NULL AS business").fetchone()[
            "business"
        ]:
            raise ValueError("agent_checkpoint_requires_dedicated_database")
        PostgresSaver(connection).setup()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_threads (
                thread_id text PRIMARY KEY,
                state jsonb NOT NULL
            )
            """
        )
        connection.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                sql.SQL(", ").join(map(sql.Identifier, TABLES)), sql.Identifier(role)
            )
        )


class PostgresThreads:
    def __init__(self, connection, saver):
        self.connection = connection
        self.saver = saver

    @classmethod
    @asynccontextmanager
    async def open(cls, dsn):
        # Keep this session for the service lifetime. The same connection
        # performs registry writes so loss of the lock also stops admission.
        async with await AsyncConnection.connect(dsn, **CONNECTION) as connection:
            row = await (
                await connection.execute(
                    "SELECT pg_try_advisory_lock(%s) AS locked", (SERVICE_LOCK,)
                )
            ).fetchone()
            if not row["locked"]:
                raise ValueError("agent_checkpoint_database_busy")
            if (
                await (
                    await connection.execute(
                        "SELECT to_regclass('core.apps') IS NOT NULL AS business"
                    )
                ).fetchone()
            )["business"]:
                raise ValueError("agent_checkpoint_requires_dedicated_database")
            async with AsyncConnectionPool(
                dsn, kwargs=CONNECTION, min_size=1, max_size=4, timeout=5, open=False
            ) as pool:
                await pool.wait(timeout=5)
                saver = AsyncPostgresSaver(
                    pool, serde=JsonPlusSerializer(allowed_msgpack_modules=None)
                )
                yield cls(connection, saver)

    async def load(self):
        rows = await (
            await self.connection.execute("SELECT thread_id, state FROM agent_threads")
        ).fetchall()
        return {row["thread_id"]: row["state"] for row in rows}

    async def save(self, thread_id, state):
        await self.connection.execute(
            """
            INSERT INTO agent_threads (thread_id, state) VALUES (%s, %s)
            ON CONFLICT (thread_id) DO UPDATE SET state = EXCLUDED.state
            """,
            (thread_id, Jsonb(state)),
        )

    async def delete(self, thread_id):
        await self.connection.execute(
            "DELETE FROM agent_threads WHERE thread_id = %s", (thread_id,)
        )

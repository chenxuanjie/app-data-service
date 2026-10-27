"""Typed board-game data; private rows belong to a dataset, public commands to an actor."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app_data_service.models import Base

SCHEMA = "board_game_agent"

public_changes = sa.Table(
    "public_changes",
    Base.metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("statistics_version", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("comments_version", sa.BigInteger, nullable=False, server_default="0"),
    schema=SCHEMA,
)


def personal(name, *columns):
    return sa.Table(
        name,
        Base.metadata,
        sa.Column("dataset_id", sa.Text, sa.ForeignKey("core.datasets.id"), primary_key=True),
        sa.Column("id", sa.Text, primary_key=True),
        *columns,
        sa.Column("row_version", sa.BigInteger, nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        schema=SCHEMA,
    )


favorites = personal(
    "favorites",
    sa.Column("game_slug", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
recent_games = personal(
    "recent_games",
    sa.Column("game_slug", sa.Text, nullable=False),
    sa.Column("visited_at", sa.DateTime(timezone=True), nullable=False),
)
preferences = personal(
    "preferences",
    sa.Column("nickname", sa.Text, nullable=False, server_default=""),
    sa.Column("voice_reply_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("assistant_mode", sa.Text, nullable=False, server_default="text_and_dictation"),
    sa.Column("game_answer_mode", sa.Text, nullable=False, server_default="knowledge_only"),
    sa.Column(
        "global_answer_mode", sa.Text, nullable=False, server_default="knowledge_then_direct"
    ),
    sa.Column("use_game_knowledge", sa.Boolean, nullable=False, server_default=sa.false()),
)
conversations = personal(
    "conversations",
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("game_id", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("opened", sa.Boolean, nullable=False, server_default=sa.true()),
)
messages = personal(
    "messages",
    sa.Column("conversation_id", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("source", sa.Text),
    sa.Column("citations", JSONB, nullable=False, server_default="[]"),
    sa.Column("evidence", JSONB, nullable=False, server_default="[]"),
    sa.Column("can_retry", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("retry_prompt", sa.Text),
)
votes = personal(
    "votes",
    sa.Column("campaign", sa.Text, nullable=False),
    sa.Column("game_slug", sa.Text, nullable=False),
)
PERSONAL = {
    t.name: t for t in (favorites, recent_games, preferences, conversations, messages, votes)
}

comments = sa.Table(
    "comments",
    Base.metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("core.users.id"), nullable=False),
    sa.Column("game_slug", sa.Text, nullable=False),
    sa.Column("author_name", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("parent_id", sa.Text, sa.ForeignKey(f"{SCHEMA}.comments.id")),
    sa.Column("root_id", sa.Text, sa.ForeignKey(f"{SCHEMA}.comments.id")),
    sa.Column("rating", sa.Integer),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
    schema=SCHEMA,
)
likes = sa.Table(
    "comment_likes",
    Base.metadata,
    sa.Column("comment_id", sa.Text, sa.ForeignKey(f"{SCHEMA}.comments.id"), primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("core.users.id"), primary_key=True),
    sa.Column("active", sa.Boolean, nullable=False),
    schema=SCHEMA,
)
receipts = sa.Table(
    "mutation_receipts",
    Base.metadata,
    sa.Column("user_id", sa.Text, sa.ForeignKey("core.users.id"), primary_key=True),
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("result", JSONB, nullable=False),
    schema=SCHEMA,
)

"""Versioned, typed library metadata. Source JSON is audit evidence only."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app_data_service.models import Base

SCHEMA = "board_game_agent"


def table(name, *columns):
    return sa.Table("library_" + name, Base.metadata, *columns, schema=SCHEMA)


def batch_key():
    return sa.Column("batch_id", sa.Uuid, primary_key=True)


def edition_fk():
    return sa.ForeignKeyConstraint(
        ["batch_id", "edition_id"],
        [f"{SCHEMA}.library_editions.batch_id", f"{SCHEMA}.library_editions.id"],
    )


batches = table(
    "batches",
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("source_hash", sa.Text, nullable=False),
    sa.Column("base_revision", sa.BigInteger, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("report", JSONB, nullable=False),
    sa.UniqueConstraint("source_hash", "base_revision"),
)
files = table(
    "files",
    sa.Column("sha256", sa.Text, primary_key=True),
    sa.Column("size", sa.BigInteger, nullable=False),
    sa.Column("storage_key", sa.Text, nullable=False, unique=True),
)
file_bodies = table(
    "file_bodies",
    sa.Column("sha256", sa.Text, sa.ForeignKey(files.c.sha256), primary_key=True),
    sa.Column("body", sa.LargeBinary, nullable=False),
    sa.Column("text_body", sa.Text),
    sa.Column("json_body", JSONB),
    sa.Column("encoding", sa.Text),
)
source_files = table(
    "source_files",
    batch_key(),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("file_sha256", sa.Text, sa.ForeignKey(files.c.sha256), nullable=False),
    sa.ForeignKeyConstraint(["batch_id"], [batches.c.id]),
)
import_runs = table(
    "import_runs",
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("batch_id", sa.Uuid, sa.ForeignKey(batches.c.id), nullable=False),
    sa.Column(
        "revision",
        sa.BigInteger,
        sa.ForeignKey(f"{SCHEMA}.library_releases.revision"),
        nullable=False,
    ),
    sa.Column("store_id", sa.Uuid, nullable=False),
    sa.Column("sql_file_max", sa.Integer, nullable=False),
    sa.Column(
        "completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
games = table(
    "games",
    batch_key(),
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("slug", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(["batch_id"], [batches.c.id]),
)
editions = table(
    "editions",
    batch_key(),
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("game_id", sa.Text, nullable=False),
    sa.Column("slug", sa.Text, nullable=False),
    sa.Column("edition_code", sa.Text, nullable=False),
    sa.Column("sort_order", sa.Integer, nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("card_accent", sa.Text, nullable=False),
    sa.Column("external_score_text", sa.Text, nullable=False),
    sa.Column("supported_players", ARRAY(sa.Integer), nullable=False),
    sa.Column("recommended_player", sa.Integer, nullable=False),
    sa.Column("manifest_titles", JSONB, nullable=False),
    sa.ForeignKeyConstraint(["batch_id", "game_id"], [games.c.batch_id, games.c.id]),
    sa.UniqueConstraint("batch_id", "slug"),
)

TEXT_FIELDS = (
    "title",
    "subtitle",
    "edition_label",
    "score_count_label",
    "release_year",
    "category_line",
    "learning_difficulty",
    "per_player_time",
    "setup_time",
    "language_requirement",
    "hero_tagline",
    "summary",
    "mentor_pitch",
    "play_time",
    "player_count",
    "complexity",
    "assistant_intro",
)
LIST_FIELDS = (
    "aliases",
    "designers",
    "publishers",
    "keywords",
    "local_aliases",
    "local_designers",
    "local_publishers",
    "local_keywords",
    "rank_badges",
    "round_flow",
    "assistant_skills",
    "quick_prompts",
)
texts = table(
    "texts",
    batch_key(),
    sa.Column("edition_id", sa.Text, primary_key=True),
    sa.Column("locale", sa.Text, primary_key=True),
    *(sa.Column(name, sa.Text, nullable=False) for name in TEXT_FIELDS),
    *(sa.Column(name, ARRAY(sa.Text), nullable=False) for name in LIST_FIELDS),
    edition_fk(),
)
categories = table(
    "categories",
    batch_key(),
    sa.Column("edition_id", sa.Text, primary_key=True),
    sa.Column("category", sa.Text, primary_key=True),
    sa.Column("position", sa.Integer, nullable=False),
    edition_fk(),
)
resources = table(
    "resources",
    batch_key(),
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("edition_id", sa.Text, nullable=False),
    *(
        sa.Column(name, sa.Text, nullable=False)
        for name in (
            "path",
            "document_type",
            "source_class",
            "origin",
            "language",
            "edition_code",
            "status",
            "review_status",
            "visibility",
            "observed_file",
        )
    ),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("ai_enabled", sa.Boolean, nullable=False),
    sa.Column("priority", sa.Integer, nullable=False),
    *(
        sa.Column(name, sa.Text)
        for name in (
            "source_url",
            "notes",
            "image_role",
            "variant",
            "legacy_document_type",
        )
    ),
    sa.Column("width", sa.Integer),
    sa.Column("height", sa.Integer),
    sa.Column("file_sha256", sa.Text, sa.ForeignKey(files.c.sha256)),
    edition_fk(),
)
derivations = table(
    "derivations",
    batch_key(),
    sa.Column("resource_id", sa.Text, primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("source_reference", sa.Text, nullable=False),
    sa.Column("resolved_source_id", sa.Text),
    sa.ForeignKeyConstraint(["batch_id", "resource_id"], [resources.c.batch_id, resources.c.id]),
    sa.ForeignKeyConstraint(
        ["batch_id", "resolved_source_id"], [resources.c.batch_id, resources.c.id]
    ),
)
bindings = table(
    "bindings",
    batch_key(),
    sa.Column("edition_id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("locale", sa.Text, primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("path", sa.Text, nullable=False),
    sa.Column("resource_id", sa.Text),
    edition_fk(),
    sa.ForeignKeyConstraint(["batch_id", "resource_id"], [resources.c.batch_id, resources.c.id]),
)
validations = table(
    "validations",
    batch_key(),
    sa.Column("edition_id", sa.Text, primary_key=True),
    sa.Column("checked_against", sa.Text, nullable=False),
    sa.Column("checked_at", sa.Text, nullable=False),
    sa.Column("issues", ARRAY(sa.Text), nullable=False),
    edition_fk(),
)
sources = table(
    "sources",
    batch_key(),
    sa.Column("path", sa.Text, primary_key=True),
    sa.Column("sha256", sa.Text, nullable=False),
    sa.Column("snapshot", JSONB, nullable=False),
    sa.ForeignKeyConstraint(["batch_id"], [batches.c.id]),
)
releases = table(
    "releases",
    sa.Column("revision", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("batch_id", sa.Uuid, sa.ForeignKey(batches.c.id), unique=True),
    sa.Column(
        "published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
entries = table(
    "entries",
    sa.Column("revision", sa.BigInteger, sa.ForeignKey(releases.c.revision), primary_key=True),
    sa.Column("edition_id", sa.Text, primary_key=True),
    sa.Column("batch_id", sa.Uuid, nullable=False),
    sa.ForeignKeyConstraint(["batch_id", "edition_id"], [editions.c.batch_id, editions.c.id]),
)
head = table(
    "head",
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision", sa.BigInteger, sa.ForeignKey(releases.c.revision), nullable=False),
)
changes = table(
    "changes",
    sa.Column("revision", sa.BigInteger, sa.ForeignKey(releases.c.revision), primary_key=True),
    sa.Column("edition_id", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
)

DRAFT_TABLES = (games, editions, texts, categories, resources, derivations, bindings, validations)

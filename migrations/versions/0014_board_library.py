"""Typed library drafts, immutable releases and a durable change cursor."""

from alembic import op

revision = "0014_board_library"
down_revision = "0013_board_public_changes"
branch_labels = depends_on = None

# Immutable migration definition, independent of evolving application metadata.
TEXT = (
    "title subtitle edition_label score_count_label release_year category_line learning_difficulty "
    "per_player_time setup_time language_requirement hero_tagline summary mentor_pitch play_time "
    "player_count complexity assistant_intro"
).split()
LISTS = (
    "aliases designers publishers keywords local_aliases local_designers local_publishers "
    "local_keywords rank_badges round_flow assistant_skills quick_prompts"
).split()


def upgrade():
    op.execute("""
    CREATE TABLE board_game_agent.library_batches (
      id UUID PRIMARY KEY, source_hash TEXT NOT NULL CHECK(source_hash ~ '^[a-f0-9]{64}$'),
      base_revision BIGINT NOT NULL CHECK(base_revision>=0),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      report JSONB NOT NULL CHECK(jsonb_typeof(report)='object'),
      UNIQUE(source_hash,base_revision));
    CREATE TABLE board_game_agent.library_files (
      sha256 TEXT PRIMARY KEY CHECK(sha256 ~ '^[a-f0-9]{64}$'),
      size BIGINT NOT NULL CHECK(size>0 AND size<=536870912),
      storage_key TEXT NOT NULL UNIQUE CHECK(storage_key ~ '^blobs/[a-f0-9]{2}/[a-f0-9]{64}$'));
    CREATE TABLE board_game_agent.library_games (
      batch_id UUID NOT NULL REFERENCES board_game_agent.library_batches(id), id TEXT NOT NULL,
      slug TEXT NOT NULL, PRIMARY KEY(batch_id,id));
    CREATE TABLE board_game_agent.library_editions (
      batch_id UUID NOT NULL, id TEXT NOT NULL, game_id TEXT NOT NULL, slug TEXT NOT NULL,
      edition_code TEXT NOT NULL, sort_order INTEGER NOT NULL, enabled BOOLEAN NOT NULL,
      card_accent TEXT NOT NULL, external_score_text TEXT NOT NULL,
      supported_players INTEGER[] NOT NULL CHECK(0<ALL(supported_players)),
      recommended_player INTEGER NOT NULL CHECK(recommended_player>=0),
      manifest_titles JSONB NOT NULL CHECK(jsonb_typeof(manifest_titles)='object'),
      PRIMARY KEY(batch_id,id), UNIQUE(batch_id,slug),
      FOREIGN KEY(batch_id,game_id) REFERENCES board_game_agent.library_games(batch_id,id));
    """)
    op.execute(
        "CREATE TABLE board_game_agent.library_texts ("
        "batch_id UUID NOT NULL, edition_id TEXT NOT NULL, locale TEXT NOT NULL, "
        + ", ".join(
            [f"{name} TEXT NOT NULL" for name in TEXT]
            + [f"{name} TEXT[] NOT NULL" for name in LISTS]
        )
        + ", PRIMARY KEY(batch_id,edition_id,locale), FOREIGN KEY(batch_id,edition_id) "
        "REFERENCES board_game_agent.library_editions(batch_id,id))"
    )
    op.execute("""
    CREATE TABLE board_game_agent.library_categories (
      batch_id UUID NOT NULL, edition_id TEXT NOT NULL, category TEXT NOT NULL,
      position INTEGER NOT NULL CHECK(position>=0), PRIMARY KEY(batch_id,edition_id,category),
      FOREIGN KEY(batch_id,edition_id) REFERENCES board_game_agent.library_editions(batch_id,id));
    CREATE TABLE board_game_agent.library_resources (
      batch_id UUID NOT NULL, id TEXT NOT NULL, edition_id TEXT NOT NULL, path TEXT NOT NULL,
      document_type TEXT NOT NULL, source_class TEXT NOT NULL, origin TEXT NOT NULL,
      language TEXT NOT NULL, edition_code TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('available','missing','superseded','blocked')),
      review_status TEXT NOT NULL,
      visibility TEXT NOT NULL CHECK(visibility IN ('public','archive')),
      observed_file TEXT NOT NULL CHECK(observed_file IN ('present','absent')),
      enabled BOOLEAN NOT NULL, ai_enabled BOOLEAN NOT NULL, priority INTEGER NOT NULL,
      source_url TEXT, notes TEXT, image_role TEXT, variant TEXT, legacy_document_type TEXT,
      width INTEGER CHECK(width>0), height INTEGER CHECK(height>0),
      file_sha256 TEXT REFERENCES board_game_agent.library_files(sha256), PRIMARY KEY(batch_id,id),
      FOREIGN KEY(batch_id,edition_id) REFERENCES board_game_agent.library_editions(batch_id,id));
    CREATE TABLE board_game_agent.library_derivations (
      batch_id UUID NOT NULL, resource_id TEXT NOT NULL,
      position INTEGER NOT NULL CHECK(position>=0),
      source_reference TEXT NOT NULL, resolved_source_id TEXT,
      PRIMARY KEY(batch_id,resource_id,position),
      FOREIGN KEY(batch_id,resource_id) REFERENCES board_game_agent.library_resources(batch_id,id),
      FOREIGN KEY(batch_id,resolved_source_id)
        REFERENCES board_game_agent.library_resources(batch_id,id));
    CREATE TABLE board_game_agent.library_bindings (
      batch_id UUID NOT NULL, edition_id TEXT NOT NULL, kind TEXT NOT NULL, locale TEXT NOT NULL,
      position INTEGER NOT NULL CHECK(position>=0), path TEXT NOT NULL, resource_id TEXT,
      PRIMARY KEY(batch_id,edition_id,kind,locale,position),
      FOREIGN KEY(batch_id,edition_id) REFERENCES board_game_agent.library_editions(batch_id,id),
      FOREIGN KEY(batch_id,resource_id) REFERENCES board_game_agent.library_resources(batch_id,id));
    CREATE TABLE board_game_agent.library_validations (
      batch_id UUID NOT NULL, edition_id TEXT NOT NULL, checked_against TEXT NOT NULL,
      checked_at TEXT NOT NULL, issues TEXT[] NOT NULL, PRIMARY KEY(batch_id,edition_id),
      FOREIGN KEY(batch_id,edition_id) REFERENCES board_game_agent.library_editions(batch_id,id));
    CREATE TABLE board_game_agent.library_sources (
      batch_id UUID NOT NULL REFERENCES board_game_agent.library_batches(id), path TEXT NOT NULL,
      sha256 TEXT NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'), snapshot JSONB NOT NULL,
      PRIMARY KEY(batch_id,path));
    CREATE TABLE board_game_agent.library_releases (
      revision BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
      batch_id UUID UNIQUE REFERENCES board_game_agent.library_batches(id),
      published_at TIMESTAMPTZ NOT NULL DEFAULT now());
    INSERT INTO board_game_agent.library_releases(revision) VALUES(0);
    CREATE TABLE board_game_agent.library_entries (
      revision BIGINT NOT NULL REFERENCES board_game_agent.library_releases(revision),
      edition_id TEXT NOT NULL, batch_id UUID NOT NULL, PRIMARY KEY(revision,edition_id),
      FOREIGN KEY(batch_id,edition_id) REFERENCES board_game_agent.library_editions(batch_id,id));
    CREATE TABLE board_game_agent.library_head (
      id INTEGER PRIMARY KEY CHECK(id=1),
      revision BIGINT NOT NULL REFERENCES board_game_agent.library_releases(revision));
    INSERT INTO board_game_agent.library_head VALUES(1,0);
    CREATE TABLE board_game_agent.library_changes (
      revision BIGINT NOT NULL REFERENCES board_game_agent.library_releases(revision),
      edition_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('upsert','unpublish')),
      PRIMARY KEY(revision,edition_id));
    CREATE INDEX library_editions_sort ON board_game_agent.library_editions(sort_order,id);
    CREATE INDEX library_resources_edition
      ON board_game_agent.library_resources(batch_id,edition_id);
    CREATE INDEX library_resources_file ON board_game_agent.library_resources(file_sha256);
    CREATE INDEX library_resources_identity ON board_game_agent.library_resources(id,edition_id);
    CREATE INDEX library_entries_batch ON board_game_agent.library_entries(batch_id,edition_id);
    CREATE INDEX library_text_title ON board_game_agent.library_texts(lower(title));
    CREATE TABLE board_game_agent.library_file_bodies (
      sha256 TEXT PRIMARY KEY REFERENCES board_game_agent.library_files(sha256),
      body BYTEA NOT NULL CHECK(octet_length(body)<=16777216),
      text_body TEXT, json_body JSONB, encoding TEXT);
    CREATE TABLE board_game_agent.library_source_files (
      batch_id UUID NOT NULL REFERENCES board_game_agent.library_batches(id), path TEXT NOT NULL,
      file_sha256 TEXT NOT NULL REFERENCES board_game_agent.library_files(sha256),
      PRIMARY KEY(batch_id,path));
    CREATE TABLE board_game_agent.library_import_runs (
      id TEXT PRIMARY KEY, batch_id UUID NOT NULL REFERENCES board_game_agent.library_batches(id),
      revision BIGINT NOT NULL REFERENCES board_game_agent.library_releases(revision),
      store_id UUID NOT NULL, sql_file_max INTEGER NOT NULL,
      completed_at TIMESTAMPTZ NOT NULL DEFAULT now());
    """)
    # Runtime is read-only. Maintenance/import uses the existing explicit maintenance
    # connection; no HTTP request gets DML rights, registration or migration privilege.
    private = ("batches", "sources", "validations", "source_files", "import_runs")
    public = (
        "files",
        "file_bodies",
        "games",
        "editions",
        "texts",
        "categories",
        "resources",
        "derivations",
        "bindings",
        "releases",
        "entries",
        "head",
        "changes",
    )
    for name in (*private, *public):
        op.execute(f"ALTER TABLE board_game_agent.library_{name} ENABLE ROW LEVEL SECURITY")
    for name in public:
        condition = "core.current_app_id()='board_game_agent' AND core.app_enabled()"
        if name in (
            "games",
            "editions",
            "texts",
            "categories",
            "resources",
            "derivations",
            "bindings",
        ):
            target = "id" if name in ("games", "editions") else "edition_id"
            if name == "games":
                relation = (
                    "JOIN board_game_agent.library_editions d "
                    "ON d.batch_id=e.batch_id AND d.id=e.edition_id"
                )
                match = "d.game_id=library_games.id AND d.enabled"
            elif name == "derivations":
                relation = (
                    "JOIN board_game_agent.library_resources r "
                    "ON r.batch_id=e.batch_id AND r.edition_id=e.edition_id"
                )
                match = "r.id=library_derivations.resource_id"
            elif name != "editions":
                relation = (
                    "JOIN board_game_agent.library_editions d "
                    "ON d.batch_id=e.batch_id AND d.id=e.edition_id"
                )
                match = f"e.edition_id=library_{name}.{target} AND d.enabled"
            else:
                relation = ""
                match = f"e.edition_id=library_{name}.{target}"
            condition += (
                " AND EXISTS(SELECT 1 FROM board_game_agent.library_entries e "
                + relation
                + " WHERE e.batch_id=library_"
                + name
                + ".batch_id AND "
                + match
                + ")"
            )
            if name == "editions":
                condition += " AND enabled"
            if name == "resources":
                condition += (
                    " AND visibility='public' AND enabled AND status='available'"
                    " AND file_sha256 IS NOT NULL"
                )
        elif name == "files":
            condition += (
                " AND EXISTS(SELECT 1 FROM board_game_agent.library_resources r "
                "WHERE r.file_sha256=library_files.sha256)"
            )
        elif name == "file_bodies":
            condition += (
                " AND EXISTS(SELECT 1 FROM board_game_agent.library_resources r "
                "WHERE r.file_sha256=library_file_bodies.sha256)"
            )
        op.execute(
            f"CREATE POLICY library_read ON board_game_agent.library_{name} FOR SELECT "
            f"TO app_runtime USING ({condition})"
        )
        op.execute(f"GRANT SELECT ON board_game_agent.library_{name} TO app_runtime")


def downgrade():
    raise RuntimeError("Library releases and imported source evidence require reviewed removal")

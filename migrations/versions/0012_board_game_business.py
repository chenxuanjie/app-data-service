"""Board-game private data, public community and signature-filtered replication."""

from alembic import op

revision = "0012_board_game_business"
down_revision = "0011_application_modules"
branch_labels = None
depends_on = None


# Immutable revision snapshot; never import evolving application models here.
TABLE_DDL = [
    (
        "favorites",
        """
CREATE TABLE board_game_agent.favorites (
	dataset_id TEXT NOT NULL,
	id TEXT NOT NULL,
	game_slug TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	row_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (dataset_id, id),
	FOREIGN KEY(dataset_id) REFERENCES core.datasets (id)
)
    """,
    ),
    (
        "recent_games",
        """
CREATE TABLE board_game_agent.recent_games (
	dataset_id TEXT NOT NULL,
	id TEXT NOT NULL,
	game_slug TEXT NOT NULL,
	visited_at TIMESTAMP WITH TIME ZONE NOT NULL,
	row_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (dataset_id, id),
	FOREIGN KEY(dataset_id) REFERENCES core.datasets (id)
)
    """,
    ),
    (
        "preferences",
        """
CREATE TABLE board_game_agent.preferences (
	dataset_id TEXT NOT NULL,
	id TEXT NOT NULL,
	nickname TEXT DEFAULT '' NOT NULL,
	voice_reply_enabled BOOLEAN DEFAULT false NOT NULL,
	assistant_mode TEXT DEFAULT 'text_and_dictation' NOT NULL,
	game_answer_mode TEXT DEFAULT 'knowledge_only' NOT NULL,
	global_answer_mode TEXT DEFAULT 'knowledge_then_direct' NOT NULL,
	use_game_knowledge BOOLEAN DEFAULT false NOT NULL,
	row_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (dataset_id, id),
	FOREIGN KEY(dataset_id) REFERENCES core.datasets (id)
)
    """,
    ),
    (
        "conversations",
        """
CREATE TABLE board_game_agent.conversations (
	dataset_id TEXT NOT NULL,
	id TEXT NOT NULL,
	title TEXT NOT NULL,
	scope TEXT NOT NULL,
	game_id TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	modified_at TIMESTAMP WITH TIME ZONE NOT NULL,
	opened BOOLEAN DEFAULT true NOT NULL,
	row_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (dataset_id, id),
	FOREIGN KEY(dataset_id) REFERENCES core.datasets (id)
)
    """,
    ),
    (
        "messages",
        """
CREATE TABLE board_game_agent.messages (
	dataset_id TEXT NOT NULL,
	id TEXT NOT NULL,
	conversation_id TEXT NOT NULL,
	role TEXT NOT NULL,
	content TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	state TEXT NOT NULL,
	source TEXT,
	citations JSONB DEFAULT '[]' NOT NULL,
	evidence JSONB DEFAULT '[]' NOT NULL,
	can_retry BOOLEAN DEFAULT false NOT NULL,
	retry_prompt TEXT,
	row_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (dataset_id, id),
	FOREIGN KEY(dataset_id) REFERENCES core.datasets (id)
)
    """,
    ),
    (
        "votes",
        """
CREATE TABLE board_game_agent.votes (
	dataset_id TEXT NOT NULL,
	id TEXT NOT NULL,
	campaign TEXT NOT NULL,
	game_slug TEXT NOT NULL,
	row_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (dataset_id, id),
	FOREIGN KEY(dataset_id) REFERENCES core.datasets (id)
)
    """,
    ),
    (
        "comments",
        """
CREATE TABLE board_game_agent.comments (
	id TEXT NOT NULL,
	user_id TEXT NOT NULL,
	game_slug TEXT NOT NULL,
	author_name TEXT NOT NULL,
	body TEXT NOT NULL,
	parent_id TEXT,
	root_id TEXT,
	rating INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES core.users (id),
	FOREIGN KEY(parent_id) REFERENCES board_game_agent.comments (id),
	FOREIGN KEY(root_id) REFERENCES board_game_agent.comments (id)
)
    """,
    ),
    (
        "comment_likes",
        """
CREATE TABLE board_game_agent.comment_likes (
	comment_id TEXT NOT NULL,
	user_id TEXT NOT NULL,
	active BOOLEAN NOT NULL,
	PRIMARY KEY (comment_id, user_id),
	FOREIGN KEY(comment_id) REFERENCES board_game_agent.comments (id),
	FOREIGN KEY(user_id) REFERENCES core.users (id)
)
    """,
    ),
    (
        "mutation_receipts",
        """
CREATE TABLE board_game_agent.mutation_receipts (
	user_id TEXT NOT NULL,
	id TEXT NOT NULL,
	payload_hash TEXT NOT NULL,
	result JSONB NOT NULL,
	PRIMARY KEY (user_id, id),
	FOREIGN KEY(user_id) REFERENCES core.users (id)
)
    """,
    ),
]


def upgrade():
    for table_name, ddl in TABLE_DDL:
        op.execute(ddl)
        name = f"board_game_agent.{table_name}"
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON {name} TO app_runtime")
        if table_name in {
            "favorites",
            "recent_games",
            "preferences",
            "conversations",
            "messages",
            "votes",
        }:
            predicate = (
                "core.current_app_id()='board_game_agent' AND core.app_enabled() "
                "AND core.can_access_dataset(dataset_id)"
            )
            op.execute(
                f"CREATE POLICY board_private ON {name} TO app_runtime "
                f"USING({predicate}) WITH CHECK({predicate})"
            )
            op.execute(f"GRANT SELECT ON {name} TO app_replication")
            op.execute(f"ALTER TABLE {name} REPLICA IDENTITY FULL")
            op.execute(f"ALTER PUBLICATION powersync ADD TABLE {name}")
        else:
            app = "core.current_app_id()='board_game_agent' AND core.app_enabled()"
            op.execute(
                f"CREATE POLICY board_owner ON {name} TO app_runtime "
                f"USING({app} AND user_id=core.actor_id()) "
                f"WITH CHECK({app} AND user_id=core.actor_id())"
            )
            if table_name == "comments":
                op.execute(
                    f"CREATE POLICY board_public_read ON {name} "
                    f"FOR SELECT TO app_runtime USING({app})"
                )
    op.execute("""
      ALTER TABLE board_game_agent.favorites ADD UNIQUE(dataset_id,game_slug);
      ALTER TABLE board_game_agent.recent_games ADD UNIQUE(dataset_id,game_slug);
      ALTER TABLE board_game_agent.votes ADD UNIQUE(dataset_id,campaign,game_slug);
      ALTER TABLE board_game_agent.messages ADD CONSTRAINT board_message_parent
        FOREIGN KEY(dataset_id,conversation_id)
        REFERENCES board_game_agent.conversations(dataset_id,id);
      ALTER TABLE board_game_agent.preferences ADD CHECK(id='profile');
      ALTER TABLE board_game_agent.preferences ADD CHECK(length(nickname)<=80);
      ALTER TABLE board_game_agent.conversations ADD CHECK(scope IN ('global','game'));
      ALTER TABLE board_game_agent.messages ADD CHECK(role IN ('user','assistant'));
      ALTER TABLE board_game_agent.messages ADD CHECK(state IN ('complete','failed'));
      ALTER TABLE board_game_agent.comments ADD CHECK(length(body) BETWEEN 1 AND 1000);
      ALTER TABLE board_game_agent.comments ADD CHECK(rating IS NULL OR rating BETWEEN 1 AND 10);
      ALTER TABLE board_game_agent.comments ADD CHECK(parent_id IS NULL OR rating IS NULL);
      CREATE INDEX board_comment_feed ON board_game_agent.comments(game_slug,created_at,id);
      CREATE INDEX board_message_conversation
        ON board_game_agent.messages(dataset_id,conversation_id);
      CREATE FUNCTION board_game_agent.public_counts() RETURNS JSONB
      LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT jsonb_build_object(
          'favorites',coalesce((SELECT jsonb_object_agg(game_slug,n) FROM (
            SELECT game_slug,count(DISTINCT d.owner_id) n FROM board_game_agent.favorites f
            JOIN core.datasets d ON d.id=f.dataset_id AND d.app_id='board_game_agent'
            WHERE f.deleted_at IS NULL GROUP BY game_slug) q),'{}'::jsonb),
          'votes',coalesce((SELECT jsonb_agg(jsonb_build_object(
            'campaign',campaign,'game_slug',game_slug,'count',n)) FROM (
            SELECT campaign,game_slug,count(DISTINCT d.owner_id) n FROM board_game_agent.votes v
            JOIN core.datasets d ON d.id=v.dataset_id AND d.app_id='board_game_agent'
            WHERE v.deleted_at IS NULL GROUP BY campaign,game_slug) q),'[]'::jsonb))
        WHERE core.current_app_id()='board_game_agent' AND core.app_enabled()
      $$;
      CREATE FUNCTION board_game_agent.like_counts(target_game TEXT)
      RETURNS TABLE(comment_id TEXT,n BIGINT)
      LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT l.comment_id,count(*) FROM board_game_agent.comment_likes l
        JOIN board_game_agent.comments c ON c.id=l.comment_id
        WHERE c.game_slug=target_game AND l.active AND c.deleted_at IS NULL
          AND core.current_app_id()='board_game_agent' AND core.app_enabled()
        GROUP BY l.comment_id
      $$;
      REVOKE ALL ON FUNCTION board_game_agent.public_counts() FROM PUBLIC;
      REVOKE ALL ON FUNCTION board_game_agent.like_counts(TEXT) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION board_game_agent.public_counts() TO app_runtime;
      GRANT EXECUTE ON FUNCTION board_game_agent.like_counts(TEXT) TO app_runtime;
    """)


def downgrade():
    raise RuntimeError("Review retained board-game data before changing its schema")

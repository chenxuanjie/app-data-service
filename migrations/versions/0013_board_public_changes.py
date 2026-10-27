"""Transactional, public-only invalidation versions for board community readers."""

from alembic import op

revision = "0013_board_public_changes"
down_revision = "0012_board_game_business"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE board_game_agent.public_changes (
        id TEXT PRIMARY KEY CHECK(id ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
        statistics_version BIGINT NOT NULL DEFAULT 0,
        comments_version BIGINT NOT NULL DEFAULT 0
      );
      ALTER TABLE board_game_agent.public_changes ENABLE ROW LEVEL SECURITY;
      CREATE POLICY board_public_read ON board_game_agent.public_changes FOR SELECT
        TO app_runtime USING(core.current_app_id()='board_game_agent' AND core.app_enabled());
      GRANT SELECT ON board_game_agent.public_changes TO app_runtime,app_replication;
      ALTER TABLE board_game_agent.public_changes REPLICA IDENTITY FULL;
      ALTER PUBLICATION powersync ADD TABLE board_game_agent.public_changes;

      CREATE FUNCTION board_game_agent.touch_public_game(game TEXT, community BOOLEAN)
      RETURNS VOID LANGUAGE SQL SECURITY DEFINER SET search_path=pg_catalog AS $$
        INSERT INTO board_game_agent.public_changes(id,statistics_version,comments_version)
        VALUES(game,CASE WHEN community THEN 0 ELSE 1 END,CASE WHEN community THEN 1 ELSE 0 END)
        ON CONFLICT(id) DO UPDATE SET
          statistics_version=board_game_agent.public_changes.statistics_version+
            excluded.statistics_version,
          comments_version=board_game_agent.public_changes.comments_version+excluded.comments_version
      $$;
      CREATE FUNCTION board_game_agent.public_change_trigger()
      RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE old_game TEXT; new_game TEXT; community BOOLEAN;
      BEGIN
        IF TG_OP='UPDATE' AND OLD IS NOT DISTINCT FROM NEW THEN RETURN NEW; END IF;
        community := TG_TABLE_NAME IN ('comments','comment_likes');
        IF TG_TABLE_NAME='comment_likes' THEN
          IF TG_OP<>'INSERT' THEN
            SELECT game_slug INTO old_game FROM board_game_agent.comments WHERE id=OLD.comment_id;
          END IF;
          IF TG_OP<>'DELETE' THEN
            SELECT game_slug INTO new_game FROM board_game_agent.comments WHERE id=NEW.comment_id;
          END IF;
        ELSE
          IF TG_OP<>'INSERT' THEN old_game := OLD.game_slug; END IF;
          IF TG_OP<>'DELETE' THEN new_game := NEW.game_slug; END IF;
        END IF;
        IF old_game IS NOT NULL THEN
          PERFORM board_game_agent.touch_public_game(old_game,community);
        END IF;
        IF new_game IS NOT NULL AND new_game IS DISTINCT FROM old_game THEN
          PERFORM board_game_agent.touch_public_game(new_game,community);
        END IF;
        RETURN NULL;
      END $$;
      REVOKE ALL ON FUNCTION board_game_agent.touch_public_game(TEXT,BOOLEAN) FROM PUBLIC;
      REVOKE ALL ON FUNCTION board_game_agent.public_change_trigger() FROM PUBLIC;
    """)
    for table in ("favorites", "votes", "comments", "comment_likes"):
        op.execute(
            f"CREATE TRIGGER board_public_change AFTER INSERT OR UPDATE OR DELETE "
            f"ON board_game_agent.{table} FOR EACH ROW "
            "EXECUTE FUNCTION board_game_agent.public_change_trigger()"
        )
    op.execute("""
      INSERT INTO board_game_agent.public_changes(id,statistics_version,comments_version)
      SELECT game_slug,1,1 FROM (
        SELECT game_slug FROM board_game_agent.favorites
        UNION SELECT game_slug FROM board_game_agent.votes
        UNION SELECT game_slug FROM board_game_agent.comments
      ) games ON CONFLICT DO NOTHING
    """)


def downgrade():
    raise RuntimeError("Review public sync subscriptions before removing their schema")

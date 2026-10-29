"""Transactional public watermarks for installer and catalog publications."""

from alembic import op

revision = "0017_publication_versions"
down_revision = "0016_release_architecture"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE core.publication_versions (
      id TEXT PRIMARY KEY,
      app_id TEXT NOT NULL REFERENCES core.apps(id),
      topic TEXT NOT NULL CHECK(topic IN ('catalog','release')),
      revision BIGINT NOT NULL CHECK(revision>=0),
      channel TEXT NOT NULL DEFAULT '',
      platform TEXT NOT NULL DEFAULT '',
      architecture TEXT NOT NULL DEFAULT '',
      UNIQUE(app_id,topic,channel,platform,architecture),
      CHECK((topic='catalog' AND channel='' AND platform='' AND architecture='') OR
        (topic='release' AND channel<>'' AND platform<>'' AND architecture<>'')));
    ALTER TABLE core.publication_versions ENABLE ROW LEVEL SECURITY;
    CREATE POLICY publication_versions_read ON core.publication_versions FOR SELECT TO app_runtime
      USING(app_id=core.current_app_id() AND core.app_enabled());
    GRANT SELECT ON core.publication_versions TO app_runtime,app_replication;
    ALTER TABLE core.publication_versions REPLICA IDENTITY FULL;
    ALTER PUBLICATION powersync ADD TABLE core.publication_versions;

    CREATE FUNCTION core.release_publication_version() RETURNS TRIGGER
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    BEGIN
      INSERT INTO core.publication_versions
        (id,app_id,topic,revision,channel,platform,architecture)
      VALUES(NEW.app_id||':release:'||NEW.channel||':'||NEW.platform||':'||NEW.architecture,
        NEW.app_id,'release',NEW.build_number,NEW.channel,NEW.platform,NEW.architecture)
      ON CONFLICT(app_id,topic,channel,platform,architecture) DO UPDATE
        SET revision=GREATEST(core.publication_versions.revision,excluded.revision);
      RETURN NULL;
    END $$;
    REVOKE ALL ON FUNCTION core.release_publication_version() FROM PUBLIC;
    CREATE TRIGGER release_publication_version AFTER INSERT ON core.public_releases
      FOR EACH ROW EXECUTE FUNCTION core.release_publication_version();

    -- Only the owner can read history during this transaction. Restore FORCE
    -- before commit; runtime publication permissions remain unchanged.
    ALTER TABLE core.public_releases NO FORCE ROW LEVEL SECURITY;
    INSERT INTO core.publication_versions(id,app_id,topic,revision,channel,platform,architecture)
      SELECT app_id||':release:'||channel||':'||platform||':'||architecture,
        app_id,'release',max(build_number),channel,platform,architecture
      FROM core.public_releases GROUP BY app_id,channel,platform,architecture;
    ALTER TABLE core.public_releases FORCE ROW LEVEL SECURITY;

    CREATE FUNCTION board_game_agent.catalog_publication_version() RETURNS TRIGGER
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
    BEGIN
      INSERT INTO core.publication_versions(id,app_id,topic,revision)
      VALUES('board_game_agent:catalog','board_game_agent','catalog',NEW.revision)
      ON CONFLICT(app_id,topic,channel,platform,architecture) DO UPDATE
        SET revision=excluded.revision;
      RETURN NULL;
    END $$;
    REVOKE ALL ON FUNCTION board_game_agent.catalog_publication_version() FROM PUBLIC;
    CREATE TRIGGER catalog_publication_version AFTER INSERT OR UPDATE
      ON board_game_agent.library_head FOR EACH ROW
      EXECUTE FUNCTION board_game_agent.catalog_publication_version();
    INSERT INTO core.publication_versions(id,app_id,topic,revision)
      SELECT 'board_game_agent:catalog','board_game_agent','catalog',revision
      FROM board_game_agent.library_head WHERE id=1;
    """)


def downgrade():
    raise RuntimeError("Review public subscriptions before removing publication watermarks")

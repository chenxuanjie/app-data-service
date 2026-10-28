"""Public installer reads and separately authorized immutable publication."""

from alembic import op

revision = "0015_public_releases"
down_revision = "0014_board_library"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE core.release_blobs (
      app_id TEXT NOT NULL REFERENCES core.apps(id),
      sha256 TEXT NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
      size BIGINT NOT NULL CHECK(size>0 AND size<=1073741824),
      PRIMARY KEY(app_id,sha256));
    CREATE TABLE core.public_releases (
      app_id TEXT NOT NULL, channel TEXT NOT NULL CHECK(channel ~ '^[a-z0-9][a-z0-9_-]{0,39}$'),
      platform TEXT NOT NULL CHECK(platform IN ('android','windows')),
      architecture TEXT NOT NULL CHECK(architecture IN ('arm64','arm','x64','x86','universal')),
      build_number BIGINT NOT NULL CHECK(build_number>0),
      version_name TEXT NOT NULL CHECK(length(version_name) BETWEEN 1 AND 80),
      minimum_build BIGINT NOT NULL CHECK(minimum_build>=0 AND minimum_build<=build_number),
      force_update BOOLEAN NOT NULL, published_at TIMESTAMPTZ NOT NULL,
      notes JSONB NOT NULL CHECK(jsonb_typeof(notes)='object'),
      sha256 TEXT NOT NULL, size BIGINT NOT NULL CHECK(size>0 AND size<=1073741824),
      mime_type TEXT NOT NULL, record JSONB NOT NULL CHECK(jsonb_typeof(record)='object'),
      PRIMARY KEY(app_id,channel,platform,architecture,build_number),
      FOREIGN KEY(app_id,sha256) REFERENCES core.release_blobs(app_id,sha256),
      CHECK(platform!='windows' OR architecture='x64'),
      CHECK((platform='android' AND mime_type='application/vnd.android.package-archive')
        OR (platform='windows' AND mime_type='application/octet-stream')));
    CREATE INDEX public_releases_file ON core.public_releases(app_id,sha256);
    ALTER TABLE core.release_blobs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE core.release_blobs FORCE ROW LEVEL SECURITY;
    ALTER TABLE core.public_releases ENABLE ROW LEVEL SECURITY;
    ALTER TABLE core.public_releases FORCE ROW LEVEL SECURITY;
    CREATE POLICY release_blobs_scope ON core.release_blobs TO app_runtime
      USING(app_id=current_setting('app.app_id',true)
        AND current_setting('app.release_publisher',true)='on'
        AND EXISTS(SELECT 1 FROM core.apps WHERE id=app_id AND enabled))
      WITH CHECK(app_id=current_setting('app.app_id',true)
        AND current_setting('app.release_publisher',true)='on'
        AND EXISTS(SELECT 1 FROM core.apps WHERE id=app_id AND enabled));
    CREATE POLICY public_releases_read ON core.public_releases FOR SELECT TO app_runtime
      USING(app_id=current_setting('app.app_id',true)
        AND EXISTS(SELECT 1 FROM core.apps WHERE id=app_id AND enabled));
    CREATE POLICY public_releases_publish ON core.public_releases FOR INSERT TO app_runtime
      WITH CHECK(app_id=current_setting('app.app_id',true)
        AND current_setting('app.release_publisher',true)='on'
        AND EXISTS(SELECT 1 FROM core.apps WHERE id=app_id AND enabled));
    GRANT SELECT,INSERT ON core.release_blobs,core.public_releases TO app_runtime;
    """)


def downgrade():
    raise RuntimeError("Public release history is retained; use a reviewed forward migration")

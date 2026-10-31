"""App-scoped build allocation and immutable multi-target release batches."""

from alembic import op

revision = "0018_release_batches"
down_revision = "0017_publication_versions"
branch_labels = depends_on = None


def upgrade():
    op.execute(r"""
    CREATE TABLE core.release_counters (
      app_id TEXT PRIMARY KEY REFERENCES core.apps(id),
      last_build BIGINT NOT NULL CHECK(last_build>=0 AND last_build<=2147483647));
    CREATE TABLE core.release_batches (
      app_id TEXT NOT NULL REFERENCES core.apps(id), id TEXT NOT NULL,
      request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 16 AND 100),
      version_name TEXT NOT NULL CHECK(version_name ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
      build_number BIGINT NOT NULL CHECK(build_number>0 AND build_number<=2147483647),
      source_revision TEXT CHECK(source_revision ~ '^[a-f0-9]{40}$'),
      dependency_revisions JSONB NOT NULL DEFAULT '{}'
        CHECK(jsonb_typeof(dependency_revisions)='object'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      frozen_at TIMESTAMPTZ,
      PRIMARY KEY(app_id,id), UNIQUE(app_id,request_key), UNIQUE(app_id,build_number),
      CHECK((source_revision IS NULL)=(frozen_at IS NULL)));
    CREATE TABLE core.release_targets (
      app_id TEXT NOT NULL, batch_id TEXT NOT NULL,
      component TEXT NOT NULL, platform TEXT NOT NULL, architecture TEXT NOT NULL,
      channel TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('built','published','failed')),
      record JSONB NOT NULL CHECK(jsonb_typeof(record)='object'),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY(app_id,batch_id,component,platform,architecture,channel),
      FOREIGN KEY(app_id,batch_id) REFERENCES core.release_batches(app_id,id));
    """)
    for table in ("release_counters", "release_batches", "release_targets"):
        op.execute(f"""
        ALTER TABLE core.{table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE core.{table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY {table}_publisher ON core.{table} TO app_runtime
          USING(app_id=core.current_app_id() AND core.app_enabled()
            AND current_setting('app.release_publisher',true)='on')
          WITH CHECK(app_id=core.current_app_id() AND core.app_enabled()
            AND current_setting('app.release_publisher',true)='on');
        GRANT SELECT,INSERT,UPDATE ON core.{table} TO app_runtime;
        """)


def downgrade():
    raise RuntimeError("Build numbers and published identities must not be recycled")

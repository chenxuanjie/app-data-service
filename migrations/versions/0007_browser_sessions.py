"""Opaque, revocable browser sessions, including in-flight login cancellation."""

from alembic import op

revision = "0007_browser_sessions"
down_revision = "0006_complete_business"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      CREATE TABLE core.browser_sessions (
        id_hash TEXT PRIMARY KEY CHECK (id_hash ~ '^[a-f0-9]{64}$'),
        refresh_ciphertext TEXT,
        login_started BOOLEAN NOT NULL DEFAULT false,
        revoked_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      );
      CREATE INDEX ix_browser_sessions_expiry ON core.browser_sessions(expires_at);
      ALTER TABLE core.browser_sessions ENABLE ROW LEVEL SECURITY;
      CREATE POLICY browser_session_token ON core.browser_sessions TO app_runtime
        USING (id_hash = current_setting('app.browser_id', true))
        WITH CHECK (id_hash = current_setting('app.browser_id', true));
      GRANT SELECT, INSERT, UPDATE ON core.browser_sessions TO app_runtime;
    """)


def downgrade():
    raise NotImplementedError("Browser session downgrade requires an explicit session migration")

"""Verified DSM sessions and isolated test dataset provisioning."""

from alembic import op

revision = "0004_account_sessions"
down_revision = "0003_sync_mutations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE core.app_sessions (
        id UUID PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES core.users(id),
        refresh_hash TEXT NOT NULL CHECK (refresh_hash ~ '^[a-f0-9]{64}$'),
        dsm_context TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      );
      CREATE INDEX ix_sessions_user ON core.app_sessions(user_id);
      ALTER TABLE core.app_sessions ENABLE ROW LEVEL SECURITY;
      CREATE POLICY sessions_owner ON core.app_sessions TO app_runtime
        USING (user_id = core.actor_id()) WITH CHECK (user_id = core.actor_id());
      GRANT SELECT, INSERT, UPDATE ON core.app_sessions TO app_runtime;

      CREATE FUNCTION core.ensure_test_account(display_name TEXT) RETURNS TEXT
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      DECLARE actor TEXT := core.actor_id(); dataset TEXT;
      BEGIN
        IF actor IS NULL OR actor !~ '^[a-f0-9]{64}$' OR
           length(display_name) NOT BETWEEN 1 AND 200 THEN
          RAISE EXCEPTION 'Verified identity required' USING ERRCODE = '42501';
        END IF;
        dataset := 'test-' || actor;
        INSERT INTO core.users(id, display_name) VALUES(actor, display_name)
          ON CONFLICT(id) DO UPDATE SET display_name = EXCLUDED.display_name;
        INSERT INTO core.datasets(id, app_id, owner_id, name)
          VALUES(dataset, 'zheri', actor, '同步测试账本') ON CONFLICT(id) DO NOTHING;
        INSERT INTO core.dataset_members(dataset_id, user_id)
          VALUES(dataset, actor) ON CONFLICT DO NOTHING;
        RETURN dataset;
      END $$;
      REVOKE ALL ON FUNCTION core.ensure_test_account(TEXT) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.ensure_test_account(TEXT) TO app_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("Sessions must not be discarded by an automatic downgrade")

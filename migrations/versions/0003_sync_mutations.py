"""Durable upload receipts and both sides of concurrent edits."""

from alembic import op

revision = "0003_sync_mutations"
down_revision = "0002_import_record_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE core.mutation_receipts (
        user_id TEXT NOT NULL REFERENCES core.users(id),
        client_id TEXT NOT NULL,
        mutation_id UUID NOT NULL,
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        record_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
        base_version BIGINT NOT NULL CHECK (base_version >= 0),
        result JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, client_id, mutation_id)
      );
      CREATE TABLE zheri.expense_conflicts (
        id UUID PRIMARY KEY,
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        user_id TEXT NOT NULL REFERENCES core.users(id),
        record_id TEXT NOT NULL,
        candidate JSONB,
        server_record JSONB,
        server_version BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolution TEXT CHECK (resolution IN ('server', 'candidate')),
        resolved_version BIGINT,
        resolved_at TIMESTAMPTZ
      );
      CREATE INDEX ix_conflicts_user_dataset ON zheri.expense_conflicts(user_id, dataset_id);
      ALTER TABLE core.mutation_receipts ENABLE ROW LEVEL SECURITY;
      ALTER TABLE zheri.expense_conflicts ENABLE ROW LEVEL SECURITY;
      CREATE POLICY receipts_owner ON core.mutation_receipts TO app_runtime
        USING (user_id = core.actor_id() AND EXISTS (
          SELECT 1 FROM core.datasets d WHERE d.id = dataset_id AND d.app_id = 'zheri'))
        WITH CHECK (user_id = core.actor_id() AND EXISTS (
          SELECT 1 FROM core.datasets d WHERE d.id = dataset_id AND d.app_id = 'zheri'));
      CREATE POLICY conflicts_owner ON zheri.expense_conflicts TO app_runtime
        USING (user_id = core.actor_id() AND EXISTS (
          SELECT 1 FROM core.datasets d WHERE d.id = dataset_id AND d.app_id = 'zheri'))
        WITH CHECK (user_id = core.actor_id() AND EXISTS (
          SELECT 1 FROM core.datasets d WHERE d.id = dataset_id AND d.app_id = 'zheri'));
      GRANT SELECT, INSERT ON core.mutation_receipts TO app_runtime;
      GRANT SELECT, INSERT, UPDATE ON zheri.expense_conflicts TO app_runtime;
    """)


def downgrade() -> None:
    raise RuntimeError("Sync receipts/conflicts must not be discarded by an automatic downgrade")

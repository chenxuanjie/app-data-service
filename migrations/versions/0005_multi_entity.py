"""Add assets/categories without rewriting expense receipts or legacy conflicts."""

from alembic import op

revision = "0005_multi_entity"
down_revision = "0004_account_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE zheri.expense_categories (
        id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 200),
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        name TEXT NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 200),
        icon_id TEXT NOT NULL CHECK(length(icon_id) BETWEEN 1 AND 200),
        sort_order BIGINT NOT NULL CHECK(sort_order BETWEEN 0 AND 2147483647),
        row_version BIGINT NOT NULL DEFAULT 1 CHECK(row_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), deleted_at TIMESTAMPTZ,
        CONSTRAINT uq_category_dataset UNIQUE(id,dataset_id)
      );
      CREATE TABLE zheri.assets (
        id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 200),
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        name TEXT NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 200),
        price NUMERIC NOT NULL CHECK(price >= 0 AND price <= 99999999999999.999999 AND
          scale(price) <= 6 AND price <> 'NaN'::numeric),
        purchase_date DATE NOT NULL CHECK(purchase_date BETWEEN DATE '0001-01-01' AND
          DATE '9999-12-31'),
        category TEXT NOT NULL CHECK(length(category) <= 200),
        residual_value NUMERIC NOT NULL CHECK(residual_value >= 0 AND residual_value <=
          price AND scale(residual_value) <= 6),
        target_daily_cost NUMERIC NOT NULL CHECK(target_daily_cost > 0 AND
          target_daily_cost <= 99999999999999.999999 AND scale(target_daily_cost) <= 6
          AND target_daily_cost <> 'NaN'::numeric),
        icon_name TEXT NOT NULL CHECK(icon_name IN ('inventory','smartphone','headphones','park')),
        color_value BIGINT NOT NULL CHECK(color_value BETWEEN 0 AND 4294967295),
        include_in_living_cost BOOLEAN NOT NULL DEFAULT false,
        note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 4000),
        sort_order BIGINT NOT NULL CHECK(sort_order BETWEEN 0 AND 2147483647),
        row_version BIGINT NOT NULL DEFAULT 1 CHECK(row_version > 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), deleted_at TIMESTAMPTZ
      );
      ALTER TABLE zheri.expenses ADD COLUMN category_id TEXT;
      ALTER TABLE zheri.expenses ADD CONSTRAINT expense_category_dataset
        FOREIGN KEY(category_id,dataset_id) REFERENCES zheri.expense_categories(id,dataset_id);
      ALTER TABLE core.mutation_receipts ADD COLUMN entity_type TEXT NOT NULL DEFAULT 'expenses'
        CHECK(entity_type IN ('expenses','assets','expense_categories'));
      CREATE TABLE zheri.sync_conflicts (
        id UUID PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        user_id TEXT NOT NULL REFERENCES core.users(id),
        entity_type TEXT NOT NULL CHECK(entity_type IN ('assets','expense_categories')),
        record_id TEXT NOT NULL, candidate JSONB, server_record JSONB,
        server_version BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolution TEXT CHECK(resolution IN ('server','candidate')), resolved_version BIGINT,
        resolved_at TIMESTAMPTZ
      );
      CREATE INDEX ix_sync_conflicts_user ON zheri.sync_conflicts(user_id,dataset_id,entity_type);
      ALTER TABLE zheri.sync_conflicts ENABLE ROW LEVEL SECURITY;
      CREATE POLICY sync_conflicts_owner ON zheri.sync_conflicts TO app_runtime
        USING(user_id=core.actor_id() AND EXISTS(SELECT 1 FROM core.datasets d WHERE
          d.id=dataset_id AND d.app_id='zheri'))
        WITH CHECK(user_id=core.actor_id() AND EXISTS(SELECT 1 FROM core.datasets d
          WHERE d.id=dataset_id AND d.app_id='zheri'));
      GRANT SELECT,INSERT,UPDATE ON zheri.sync_conflicts TO app_runtime;
    """)
    for table in ("assets", "expense_categories"):
        # Fixed identifiers, never derived from a request.
        op.execute(f"""
          CREATE INDEX ix_{table}_dataset_order ON zheri.{table}(dataset_id,sort_order,id);
          ALTER TABLE zheri.{table} ENABLE ROW LEVEL SECURITY;
          CREATE POLICY {table}_member ON zheri.{table} TO app_runtime
            USING(EXISTS(SELECT 1 FROM core.dataset_members m JOIN core.datasets d
              ON d.id=m.dataset_id
              WHERE m.dataset_id={table}.dataset_id AND m.user_id=core.actor_id()
                AND d.app_id='zheri'))
            WITH CHECK(EXISTS(SELECT 1 FROM core.dataset_members m JOIN
              core.datasets d ON d.id=m.dataset_id
              WHERE m.dataset_id={table}.dataset_id AND m.user_id=core.actor_id()
                AND d.app_id='zheri'));
          CREATE TRIGGER {table}_stamp BEFORE INSERT OR UPDATE ON zheri.{table}
            FOR EACH ROW EXECUTE FUNCTION zheri.stamp_expense();
          GRANT SELECT,INSERT,UPDATE ON zheri.{table} TO app_runtime;
          GRANT SELECT ON zheri.{table} TO app_replication;
          ALTER PUBLICATION powersync ADD TABLE zheri.{table};
        """)


def downgrade() -> None:
    raise RuntimeError("Do not automatically discard synchronized business records")

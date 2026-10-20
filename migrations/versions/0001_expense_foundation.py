"""Initial private datasets, expenses, import receipts and PowerSync publication."""

from alembic import op

revision = "0001_expense_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      REVOKE ALL ON SCHEMA core FROM PUBLIC;
      CREATE SCHEMA zheri;
      REVOKE ALL ON SCHEMA zheri FROM PUBLIC;

      CREATE TABLE core.apps (id TEXT PRIMARY KEY, name TEXT NOT NULL);
      INSERT INTO core.apps (id, name) VALUES ('zheri', '折日');
      CREATE TABLE core.users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL);
      CREATE TABLE core.datasets (
        id TEXT PRIMARY KEY,
        app_id TEXT NOT NULL REFERENCES core.apps(id),
        owner_id TEXT NOT NULL REFERENCES core.users(id),
        name TEXT NOT NULL
      );
      CREATE TABLE core.dataset_members (
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        user_id TEXT NOT NULL REFERENCES core.users(id),
        PRIMARY KEY (dataset_id, user_id)
      );
      CREATE TABLE zheri.expenses (
        id TEXT PRIMARY KEY,
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        name TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        cycle TEXT NOT NULL,
        category TEXT NOT NULL,
        subcategory TEXT NOT NULL,
        icon_id TEXT,
        include_in_living_cost BOOLEAN NOT NULL DEFAULT true,
        note TEXT NOT NULL DEFAULT '',
        sort_order BIGINT NOT NULL,
        row_version BIGINT NOT NULL DEFAULT 1,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        deleted_at TIMESTAMPTZ,
        CONSTRAINT expense_id_length CHECK (length(id) BETWEEN 1 AND 200),
        CONSTRAINT expense_name_length CHECK (length(btrim(name)) BETWEEN 1 AND 200),
        CONSTRAINT expense_amount_range CHECK (
          amount >= 0 AND amount <= 99999999999999.999999
          AND scale(amount) <= 6 AND amount <> 'NaN'::numeric
        ),
        CONSTRAINT expense_cycle CHECK (
          cycle IN ('daily','weekly','monthly','quarterly','halfYearly','yearly')
        ),
        CONSTRAINT expense_category_length CHECK (length(category) <= 200),
        CONSTRAINT expense_subcategory_length CHECK (length(subcategory) <= 200),
        CONSTRAINT expense_icon_length CHECK (icon_id IS NULL OR length(icon_id) <= 200),
        CONSTRAINT expense_note_length CHECK (length(note) <= 4000),
        CONSTRAINT expense_sort_order CHECK (sort_order BETWEEN 0 AND 2147483647),
        CONSTRAINT expense_row_version CHECK (row_version > 0)
      );
      CREATE INDEX ix_expenses_dataset_order ON zheri.expenses(dataset_id, sort_order, id);
      CREATE TABLE core.import_batches (
        id TEXT PRIMARY KEY,
        dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
        source_hash TEXT NOT NULL,
        source_count INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_import_dataset_hash UNIQUE (dataset_id, source_hash),
        CONSTRAINT import_source_count CHECK (source_count >= 0),
        CONSTRAINT import_source_hash CHECK (source_hash ~ '^[0-9a-f]{64}$')
      );
      CREATE TABLE core.import_records (
        batch_id TEXT NOT NULL REFERENCES core.import_batches(id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL REFERENCES zheri.expenses(id),
        PRIMARY KEY (batch_id, source_id),
        CONSTRAINT uq_import_target UNIQUE (batch_id, target_id)
      );

      CREATE FUNCTION core.actor_id() RETURNS TEXT LANGUAGE SQL STABLE AS
        $$ SELECT nullif(current_setting('app.user_id', true), '') $$;
      REVOKE ALL ON FUNCTION core.actor_id() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.actor_id() TO app_runtime;

      ALTER TABLE core.users ENABLE ROW LEVEL SECURITY;
      ALTER TABLE core.datasets ENABLE ROW LEVEL SECURITY;
      ALTER TABLE core.dataset_members ENABLE ROW LEVEL SECURITY;
      ALTER TABLE core.import_batches ENABLE ROW LEVEL SECURITY;
      ALTER TABLE core.import_records ENABLE ROW LEVEL SECURITY;
      ALTER TABLE zheri.expenses ENABLE ROW LEVEL SECURITY;
      CREATE POLICY users_self ON core.users FOR SELECT TO app_runtime
        USING (id = core.actor_id());
      CREATE POLICY memberships_self ON core.dataset_members FOR SELECT TO app_runtime
        USING (user_id = core.actor_id());
      CREATE POLICY datasets_member ON core.datasets FOR SELECT TO app_runtime USING (
        EXISTS (SELECT 1 FROM core.dataset_members m
                WHERE m.dataset_id = id AND m.user_id = core.actor_id())
      );
      CREATE POLICY expenses_member ON zheri.expenses TO app_runtime USING (
        EXISTS (SELECT 1 FROM core.dataset_members m JOIN core.datasets d ON d.id=m.dataset_id
                WHERE m.dataset_id = expenses.dataset_id AND m.user_id = core.actor_id()
                AND d.app_id = 'zheri')
      ) WITH CHECK (
        EXISTS (SELECT 1 FROM core.dataset_members m JOIN core.datasets d ON d.id=m.dataset_id
                WHERE m.dataset_id = expenses.dataset_id AND m.user_id = core.actor_id()
                AND d.app_id = 'zheri')
      );
      CREATE POLICY imports_member ON core.import_batches TO app_runtime USING (
        EXISTS (SELECT 1 FROM core.dataset_members m JOIN core.datasets d ON d.id=m.dataset_id
                WHERE m.dataset_id = import_batches.dataset_id
                AND m.user_id = core.actor_id() AND d.app_id = 'zheri')
      ) WITH CHECK (
        EXISTS (SELECT 1 FROM core.dataset_members m JOIN core.datasets d ON d.id=m.dataset_id
                WHERE m.dataset_id = import_batches.dataset_id
                AND m.user_id = core.actor_id() AND d.app_id = 'zheri')
      );
      CREATE POLICY import_records_member ON core.import_records TO app_runtime USING (
        EXISTS (SELECT 1 FROM core.import_batches b WHERE b.id = batch_id)
      ) WITH CHECK (
        EXISTS (SELECT 1 FROM core.import_batches b WHERE b.id = batch_id)
      );

      CREATE FUNCTION zheri.stamp_expense() RETURNS TRIGGER LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'INSERT' THEN
          NEW.row_version := 1;
        ELSE
          IF NEW.id <> OLD.id OR NEW.dataset_id <> OLD.dataset_id THEN
            RAISE EXCEPTION 'Expense identity and dataset are immutable' USING ERRCODE='23514';
          END IF;
          NEW.row_version := OLD.row_version + 1;
        END IF;
        NEW.updated_at := clock_timestamp();
        RETURN NEW;
      END $$;
      REVOKE ALL ON FUNCTION zheri.stamp_expense() FROM PUBLIC;
      CREATE TRIGGER expense_stamp BEFORE INSERT OR UPDATE ON zheri.expenses
        FOR EACH ROW EXECUTE FUNCTION zheri.stamp_expense();

      GRANT USAGE ON SCHEMA core, zheri TO app_runtime, app_replication;
      GRANT SELECT ON core.apps, core.users, core.datasets, core.dataset_members TO app_runtime;
      GRANT SELECT, INSERT, UPDATE ON zheri.expenses TO app_runtime;
      GRANT SELECT, INSERT ON core.import_batches, core.import_records TO app_runtime;
      GRANT SELECT ON zheri.expenses, core.datasets, core.dataset_members TO app_replication;
      CREATE PUBLICATION powersync FOR TABLE zheri.expenses, core.datasets, core.dataset_members;
    """)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql("SELECT EXISTS(SELECT 1 FROM core.users)").scalar_one():
        raise RuntimeError("Refusing to downgrade a database containing user data")
    op.execute("""
      DROP PUBLICATION powersync;
      DROP TABLE core.import_records, core.import_batches;
      DROP TABLE zheri.expenses;
      DROP FUNCTION zheri.stamp_expense();
      DROP SCHEMA zheri;
      DROP TABLE core.dataset_members, core.datasets, core.users, core.apps;
      DROP FUNCTION core.actor_id();
    """)

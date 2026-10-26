"""Shared accounts, independent app sessions and data scopes; preserve Zheri IDs."""

from alembic import op

revision = "0010_application_scope"
down_revision = "0009_preference_edit_time"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      INSERT INTO core.apps(id,name) VALUES('board_game_agent','桌游助手');
      CREATE SCHEMA board_game_agent;
      REVOKE ALL ON SCHEMA board_game_agent FROM PUBLIC;
      GRANT USAGE ON SCHEMA board_game_agent TO app_runtime,app_replication;

      CREATE FUNCTION core.current_app_id() RETURNS TEXT LANGUAGE SQL STABLE
        SET search_path=pg_catalog AS
        $$ SELECT nullif(current_setting('app.app_id',true),'') $$;
      REVOKE ALL ON FUNCTION core.current_app_id() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.current_app_id() TO app_runtime;

      ALTER TABLE core.app_sessions ADD COLUMN app_id TEXT NOT NULL DEFAULT 'zheri'
        REFERENCES core.apps(id);
      ALTER TABLE core.app_sessions ALTER COLUMN app_id DROP DEFAULT;
      ALTER TABLE core.browser_sessions ADD COLUMN app_id TEXT NOT NULL DEFAULT 'zheri'
        REFERENCES core.apps(id);
      ALTER TABLE core.browser_sessions ALTER COLUMN app_id DROP DEFAULT;
      CREATE POLICY sessions_app ON core.app_sessions AS RESTRICTIVE TO app_runtime
        USING(app_id=core.current_app_id()) WITH CHECK(app_id=core.current_app_id());
      CREATE POLICY browser_sessions_app ON core.browser_sessions AS RESTRICTIVE TO app_runtime
        USING(app_id=core.current_app_id()) WITH CHECK(app_id=core.current_app_id());

      -- SECURITY DEFINER avoids recursive RLS between datasets and memberships.
      -- It returns a boolean only, uses no dynamic SQL and never accepts an actor/app override.
      CREATE FUNCTION core.can_access_dataset(target TEXT) RETURNS BOOLEAN
        LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
          SELECT EXISTS(SELECT 1 FROM core.datasets d JOIN core.dataset_members m
            ON m.dataset_id=d.id WHERE d.id=target AND d.app_id=core.current_app_id()
            AND m.user_id=core.actor_id())
        $$;
      REVOKE ALL ON FUNCTION core.can_access_dataset(TEXT) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.can_access_dataset(TEXT) TO app_runtime;
      DROP POLICY datasets_member ON core.datasets;
      CREATE POLICY datasets_member ON core.datasets FOR SELECT TO app_runtime
        USING(core.can_access_dataset(id));
      DROP POLICY memberships_self ON core.dataset_members;
      CREATE POLICY memberships_self ON core.dataset_members FOR SELECT TO app_runtime
        USING(user_id=core.actor_id() AND core.can_access_dataset(dataset_id));

      DROP FUNCTION core.ensure_personal_account(TEXT);
      CREATE FUNCTION core.ensure_app_account(display_name TEXT) RETURNS TEXT
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE actor TEXT:=core.actor_id(); application TEXT:=core.current_app_id(); dataset TEXT;
      BEGIN
        IF actor IS NULL OR actor !~ '^[a-f0-9]{64}$' OR display_name IS NULL OR
           length(display_name) NOT BETWEEN 1 AND 200 OR application IS NULL OR
           application NOT IN ('zheri','board_game_agent') THEN
          RAISE EXCEPTION 'Verified application identity required' USING ERRCODE='42501';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(actor,102));
        INSERT INTO core.users(id,display_name) VALUES(actor,display_name)
          ON CONFLICT(id) DO UPDATE SET display_name=EXCLUDED.display_name;
        IF application='zheri' THEN
          SELECT id INTO dataset FROM core.datasets WHERE app_id=application AND owner_id=actor
            AND id IN ('test-'||actor,'personal-'||actor) ORDER BY id DESC LIMIT 1;
          IF dataset IS NOT NULL THEN
            UPDATE core.datasets SET kind='personal',name='个人账本' WHERE id=dataset;
          END IF;
        ELSE
          SELECT id INTO dataset FROM core.datasets WHERE app_id=application AND owner_id=actor
            AND id='personal-'||application||'-'||actor;
        END IF;
        IF dataset IS NULL THEN
          dataset:=CASE WHEN application='zheri' THEN 'personal-'||actor
            ELSE 'personal-'||application||'-'||actor END;
          INSERT INTO core.datasets(id,app_id,owner_id,name,kind)
            VALUES(dataset,application,actor,
              CASE WHEN application='zheri' THEN '个人账本' ELSE '个人数据' END,'personal');
        END IF;
        INSERT INTO core.dataset_members(dataset_id,user_id) VALUES(dataset,actor)
          ON CONFLICT DO NOTHING;
        RETURN dataset;
      END $$;
      REVOKE ALL ON FUNCTION core.ensure_app_account(TEXT) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.ensure_app_account(TEXT) TO app_runtime;
    """)
    # Domain tables and receipts also fail closed if a future query omits its app filter.
    for table in (
        "zheri.expenses",
        "zheri.assets",
        "zheri.expense_categories",
        "zheri.payroll_records",
        "zheri.asset_allocations",
        "zheri.business_preferences",
        "zheri.expense_conflicts",
        "zheri.sync_conflicts",
        "core.import_batches",
        "core.import_records",
        "core.mutation_receipts",
    ):
        op.execute(f"""
          CREATE POLICY zheri_application ON {table} AS RESTRICTIVE TO app_runtime
            USING(core.current_app_id()='zheri') WITH CHECK(core.current_app_id()='zheri');
        """)


def downgrade():
    raise RuntimeError("Application isolation cannot be removed by a destructive downgrade")

"""Generic registered applications and primary personal spaces; no business-name branching."""

from alembic import op

revision = "0011_application_modules"
down_revision = "0010_application_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      -- Domain-owned receipts keep their rows, hashes, foreign keys, policies and privileges.
      ALTER TABLE core.import_batches SET SCHEMA zheri;
      ALTER TABLE core.import_records SET SCHEMA zheri;
      ALTER TABLE core.mutation_receipts SET SCHEMA zheri;
      ALTER TABLE core.apps ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT true;
      ALTER TABLE core.apps ADD COLUMN schema_name TEXT;
      ALTER TABLE core.apps ADD COLUMN personal_data BOOLEAN NOT NULL DEFAULT true;
      ALTER TABLE core.apps ADD COLUMN personal_space_name TEXT NOT NULL DEFAULT '个人数据';
      ALTER TABLE core.apps ADD COLUMN personal_id_prefix TEXT
        CHECK(personal_id_prefix IS NULL OR personal_id_prefix ~ '^[a-z][a-z0-9_-]{0,63}$');
      UPDATE core.apps SET schema_name=id;
      UPDATE core.apps SET personal_space_name='个人账本',personal_id_prefix='personal-'
        WHERE id='zheri';
      ALTER TABLE core.apps ALTER COLUMN schema_name SET NOT NULL;
      ALTER TABLE core.apps ADD CHECK(schema_name ~ '^[a-z][a-z0-9_]{1,63}$');
      CREATE UNIQUE INDEX uq_apps_business_schema ON core.apps(schema_name);
      CREATE UNIQUE INDEX uq_apps_personal_prefix ON core.apps(personal_id_prefix)
        WHERE personal_id_prefix IS NOT NULL;

      ALTER TABLE core.datasets ADD CONSTRAINT uq_dataset_app_owner UNIQUE(id,app_id,owner_id);
      CREATE TABLE core.personal_spaces(
        app_id TEXT NOT NULL REFERENCES core.apps(id),
        user_id TEXT NOT NULL REFERENCES core.users(id),
        dataset_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY(app_id,user_id),
        FOREIGN KEY(dataset_id,app_id,user_id) REFERENCES core.datasets(id,app_id,owner_id)
      );
      -- Preserve existing identities, including the established test-* ledger IDs.
      INSERT INTO core.personal_spaces(app_id,user_id,dataset_id)
        SELECT DISTINCT ON(app_id,owner_id) app_id,owner_id,id FROM core.datasets
        ORDER BY app_id,owner_id,
          CASE WHEN id='test-'||owner_id THEN 0
               WHEN id='personal-'||owner_id THEN 1 ELSE 2 END,id;
      ALTER TABLE core.personal_spaces ENABLE ROW LEVEL SECURITY;
      CREATE POLICY personal_space_self ON core.personal_spaces FOR SELECT TO app_runtime
        USING(user_id=core.actor_id() AND app_id=core.current_app_id()
          AND core.can_access_dataset(dataset_id));
      GRANT SELECT ON core.personal_spaces TO app_runtime;

      CREATE FUNCTION core.app_enabled() RETURNS BOOLEAN LANGUAGE SQL STABLE
        SET search_path=pg_catalog AS $$
          SELECT EXISTS(SELECT 1 FROM core.apps WHERE id=core.current_app_id() AND enabled)
        $$;
      REVOKE ALL ON FUNCTION core.app_enabled() FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.app_enabled() TO app_runtime;
      CREATE OR REPLACE FUNCTION core.can_access_dataset(target TEXT) RETURNS BOOLEAN
        LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
          SELECT EXISTS(SELECT 1 FROM core.datasets d JOIN core.dataset_members m
            ON m.dataset_id=d.id JOIN core.apps a ON a.id=d.app_id
            WHERE d.id=target AND d.app_id=core.current_app_id()
            AND m.user_id=core.actor_id() AND a.enabled)
        $$;
      CREATE POLICY session_app_enabled ON core.app_sessions AS RESTRICTIVE TO app_runtime
        USING(core.app_enabled()) WITH CHECK(core.app_enabled());
      CREATE POLICY browser_app_enabled ON core.browser_sessions AS RESTRICTIVE TO app_runtime
        USING(core.app_enabled()) WITH CHECK(core.app_enabled());

      CREATE OR REPLACE FUNCTION core.ensure_app_account(display_name TEXT) RETURNS TEXT
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE actor TEXT:=core.actor_id(); application TEXT:=core.current_app_id();
        dataset TEXT; definition core.apps%ROWTYPE;
      BEGIN
        SELECT * INTO definition FROM core.apps WHERE id=application AND enabled;
        IF NOT FOUND OR actor IS NULL OR actor !~ '^[a-f0-9]{64}$' OR display_name IS NULL
          OR length(display_name) NOT BETWEEN 1 AND 200 THEN
          RAISE EXCEPTION 'Verified registered application required' USING ERRCODE='42501';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(actor,102));
        INSERT INTO core.users(id,display_name) VALUES(actor,display_name)
          ON CONFLICT(id) DO UPDATE SET display_name=EXCLUDED.display_name;
        IF NOT definition.personal_data THEN RETURN NULL; END IF;
        SELECT dataset_id INTO dataset FROM core.personal_spaces
          WHERE app_id=application AND user_id=actor;
        IF dataset IS NULL THEN
          dataset:=coalesce(definition.personal_id_prefix,'personal-'||application||'-')||actor;
          INSERT INTO core.datasets(id,app_id,owner_id,name,kind)
            VALUES(dataset,application,actor,definition.personal_space_name,'personal');
          INSERT INTO core.personal_spaces(app_id,user_id,dataset_id)
            VALUES(application,actor,dataset);
        END IF;
        INSERT INTO core.dataset_members(dataset_id,user_id) VALUES(dataset,actor)
          ON CONFLICT DO NOTHING;
        RETURN dataset;
      END $$;
    """)


def downgrade():
    raise RuntimeError(
        "Registered application data must be retained; use an audited forward migration"
    )

"""Finish staged dataset state in place, retaining identities and queues."""

from alembic import op

revision = "0008_personal_ledgers"
down_revision = "0007_browser_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
      UPDATE core.datasets SET kind='personal',name='个人账本'
        WHERE app_id='zheri' AND kind='trial';
      ALTER TABLE core.datasets ALTER COLUMN kind SET DEFAULT 'personal';
      DROP FUNCTION core.promote_personal_dataset(TEXT);
      DROP FUNCTION core.ensure_test_account(TEXT);
    """)
    # Existing IDs (including test-*) remain unchanged. New sign-ins also finish
    # old state in place if an older provisioning process created it meanwhile.
    op.execute("""
      CREATE OR REPLACE FUNCTION core.ensure_personal_account(display_name TEXT) RETURNS TEXT
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE actor TEXT:=core.actor_id(); dataset TEXT;
      BEGIN
        IF actor IS NULL OR actor !~ '^[a-f0-9]{64}$' OR
          length(display_name) NOT BETWEEN 1 AND 200 THEN
          RAISE EXCEPTION 'Verified identity required' USING ERRCODE='42501';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(actor,102));
        SELECT id INTO dataset FROM core.datasets WHERE app_id='zheri' AND owner_id=actor
          AND id IN ('test-'||actor,'personal-'||actor) ORDER BY id DESC LIMIT 1;
        INSERT INTO core.users(id,display_name) VALUES(actor,display_name)
          ON CONFLICT(id) DO UPDATE SET display_name=EXCLUDED.display_name;
        IF dataset IS NULL THEN
          dataset:='personal-'||actor;
          INSERT INTO core.datasets(id,app_id,owner_id,name,kind)
            VALUES(dataset,'zheri',actor,'个人账本','personal');
        ELSE
          UPDATE core.datasets SET kind='personal',name='个人账本' WHERE id=dataset;
        END IF;
        INSERT INTO core.dataset_members(dataset_id,user_id) VALUES(dataset,actor)
          ON CONFLICT DO NOTHING;
        RETURN dataset;
      END $$;
    """)


def downgrade():
    raise RuntimeError("Do not revert personal-ledger state or discard pending mutations")

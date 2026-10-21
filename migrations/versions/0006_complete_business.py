"""Complete business replication and explicit in-place personal-ledger promotion."""

from alembic import op

revision = "0006_complete_business"
down_revision = "0005_multi_entity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(r"""
CREATE FUNCTION zheri.valid_business_document(kind TEXT, doc JSONB) RETURNS BOOLEAN
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $fn$
DECLARE fields JSONB := $schemas${
  "payroll_records": {
    "monthKey": "month",
    "salary": "money",
    "personalConsumption": "money",
    "foodBudget": "money",
    "rentProperty": "money",
    "parentSupport": "money",
    "allocations": "allocations",
    "recordedAt": "timestamp",
    "fixedSavings": "money",
    "investmentRatio": "ratio",
    "savingsRatio": "ratio",
    "note": "note",
    "updatedAt": "nullable_timestamp"
  },
  "asset_allocations": {
    "color": "color",
    "currentAmount": "money",
    "ruleType": "rule",
    "ruleRatio": "ratio",
    "historicalHighPrice": "money",
    "addPositionSteps": "steps"
  },
  "payroll_defaults": {
    "personalConsumption": "money",
    "foodBudget": "money",
    "parentSupport": "money",
    "rentProperty": "nullable_money",
    "investmentRatio": "ratio",
    "savingsRatio": "ratio",
    "allocations": "allocations"
  },
  "allocation": {
    "categoryId": "text",
    "categoryName": "text",
    "ratio": "ratio",
    "amount": "money",
    "colorValue": "color"
  },
  "step": {
    "id": "identifier",
    "drawdownRatio": "ratio",
    "amount": "money"
  }
}$schemas$::jsonb -> kind;
  value TEXT; k TEXT; subtype TEXT; item JSONB; seen TEXT[] := '{}'; identity TEXT;
BEGIN
  IF doc IS NULL THEN RETURN false; END IF;
  IF kind LIKE 'nullable_%' THEN
    RETURN doc='null'::jsonb OR zheri.valid_business_document(substr(kind,10),doc);
  END IF;
  value := doc #>> '{}';
  IF kind IN ('money','ratio') THEN
    IF jsonb_typeof(doc)<>'string' THEN RETURN false; END IF;
    IF kind='money' THEN RETURN value ~ '^(0|[1-9][0-9]{0,13})(\.[0-9]{1,6})?$'; END IF;
    RETURN value ~ '^(0|[1-9][0-9]{0,13})(\.[0-9]{1,18})?$' AND value::numeric<=1;
  ELSIF kind IN ('text','note','identifier') THEN
    RETURN jsonb_typeof(doc)='string' AND length(value)<=CASE WHEN kind='note' THEN 4000 ELSE
      200 END
      AND (kind<>'identifier' OR length(btrim(value))>0);
  ELSIF kind='color' THEN
    RETURN jsonb_typeof(doc)='number' AND value ~ '^[0-9]+$' AND value::numeric BETWEEN 0 AND
      4294967295;
  ELSIF kind='boolean' THEN RETURN jsonb_typeof(doc)='boolean';
  ELSIF kind='rule' THEN RETURN jsonb_typeof(doc)='string' AND value IN
      ('target','minimum','maximum');
  ELSIF kind='month' THEN RETURN jsonb_typeof(doc)='string' AND
    value ~ '^[0-9]{4}-(0[1-9]|1[0-2])$' AND left(value,4)<>'0000';
  ELSIF kind='timestamp' THEN
    IF jsonb_typeof(doc)<>'string' OR length(value)>40 OR value NOT LIKE '%T%' OR
      value !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN RETURN false; END IF;
    RETURN extract(year FROM value::timestamptz) BETWEEN 1 AND 9999;
  ELSIF kind IN ('steps','allocations','ids') THEN
    IF jsonb_typeof(doc)<>'array' OR jsonb_array_length(doc)>1000 THEN RETURN false; END IF;
    subtype := CASE kind WHEN 'steps' THEN 'step' WHEN 'ids' THEN 'identifier' ELSE
      'allocation' END;
    FOR item IN SELECT * FROM jsonb_array_elements(doc) LOOP
      IF NOT zheri.valid_business_document(subtype,item) THEN RETURN false; END IF;
      IF kind IN ('steps','ids') THEN
        identity := CASE kind WHEN 'steps' THEN item->>'id' ELSE item#>>'{}' END;
        IF identity=ANY(seen) THEN RETURN false; END IF;
        seen := array_append(seen,identity);
      END IF;
    END LOOP;
    RETURN true;
  END IF;
  IF fields IS NULL OR jsonb_typeof(doc)<>'object' THEN RETURN false; END IF;
  IF (SELECT count(*) FROM jsonb_object_keys(doc))<>(SELECT count(*) FROM
      jsonb_object_keys(fields)) THEN
    RETURN false;
  END IF;
  FOR k,subtype IN SELECT f.key,f.value FROM jsonb_each_text(fields) AS f LOOP
    IF NOT doc ? k OR NOT zheri.valid_business_document(subtype,doc->k) THEN RETURN false; END IF;
  END LOOP;
  RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END $fn$;
REVOKE ALL ON FUNCTION zheri.valid_business_document(TEXT,JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION zheri.valid_business_document(TEXT,JSONB) TO app_runtime;
""")
    for table in ("payroll_records", "asset_allocations", "business_preferences"):
        kind = (
            "CASE name WHEN 'include_asset_costs' THEN 'boolean' "
            "WHEN 'allocation_baseline' THEN 'money' WHEN 'allocation_order' THEN 'ids' "
            "WHEN 'payroll_defaults' THEN 'payroll_defaults' ELSE 'invalid' END"
            if table == "business_preferences"
            else "'" + table + "'"
        )
        extra = ", UNIQUE(dataset_id,name)" if table != "asset_allocations" else ""
        if table == "payroll_records":
            extra += ", CHECK(name=payload->>'monthKey')"
        op.execute(f"""
          CREATE TABLE zheri.{table} (
            id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 200),
            dataset_id TEXT NOT NULL REFERENCES core.datasets(id),
            name TEXT NOT NULL CHECK(length(btrim(name)) BETWEEN 1 AND 200),
            payload JSONB NOT NULL CHECK(zheri.valid_business_document({kind},payload)),
            sort_order BIGINT NOT NULL CHECK(sort_order BETWEEN 0 AND 2147483647),
            row_version BIGINT NOT NULL DEFAULT 1 CHECK(row_version>0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), deleted_at TIMESTAMPTZ
            {extra}
          );
          CREATE INDEX ix_{table}_dataset ON zheri.{table}(dataset_id,sort_order,id);
          ALTER TABLE zheri.{table} ENABLE ROW LEVEL SECURITY;
          CREATE POLICY {table}_member ON zheri.{table} TO app_runtime
            USING(EXISTS(SELECT 1 FROM core.dataset_members m JOIN core.datasets d ON
      d.id=m.dataset_id
              WHERE m.dataset_id={table}.dataset_id AND m.user_id=core.actor_id() AND
      d.app_id='zheri'))
            WITH CHECK(EXISTS(SELECT 1 FROM core.dataset_members m JOIN core.datasets d ON
      d.id=m.dataset_id
              WHERE m.dataset_id={table}.dataset_id AND m.user_id=core.actor_id() AND
      d.app_id='zheri'));
          CREATE TRIGGER {table}_stamp BEFORE INSERT OR UPDATE ON zheri.{table}
            FOR EACH ROW EXECUTE FUNCTION zheri.stamp_expense();
          GRANT SELECT,INSERT,UPDATE ON zheri.{table} TO app_runtime;
          GRANT SELECT ON zheri.{table} TO app_replication;
          ALTER PUBLICATION powersync ADD TABLE zheri.{table};
        """)
    entities = (
        "'expenses','assets','expense_categories','payroll_records',"
        "'asset_allocations','business_preferences'"
    )
    for table in ("core.mutation_receipts", "zheri.sync_conflicts"):
        constraint = table.split(".")[1] + "_entity_type_check"
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        allowed = entities if table.startswith("core") else entities.replace("'expenses',", "")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK(entity_type IN ({allowed}))"
        )
    op.execute("""
      ALTER TABLE core.datasets ADD COLUMN kind TEXT NOT NULL DEFAULT 'trial'
        CHECK(kind IN ('trial','personal'));
      CREATE FUNCTION core.ensure_personal_account(display_name TEXT) RETURNS TEXT
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      DECLARE actor TEXT:=core.actor_id(); dataset TEXT;
      BEGIN
        IF actor IS NULL OR actor !~ '^[a-f0-9]{64}$' OR length(display_name) NOT BETWEEN 1 AND
      200 THEN
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
        END IF;
        INSERT INTO core.dataset_members(dataset_id,user_id) VALUES(dataset,actor) ON CONFLICT
      DO NOTHING;
        RETURN dataset;
      END $$;
      CREATE FUNCTION core.promote_personal_dataset(target TEXT) RETURNS TEXT
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
      BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended(target,101));
        UPDATE core.datasets SET kind='personal',name='个人账本'
          WHERE id=target AND owner_id=core.actor_id() AND app_id='zheri';
        IF NOT FOUND THEN RAISE EXCEPTION 'Dataset unavailable' USING ERRCODE='42501'; END IF;
        RETURN target;
      END $$;
      REVOKE ALL ON FUNCTION core.ensure_personal_account(TEXT) FROM PUBLIC;
      REVOKE ALL ON FUNCTION core.promote_personal_dataset(TEXT) FROM PUBLIC;
      GRANT EXECUTE ON FUNCTION core.ensure_personal_account(TEXT) TO app_runtime;
      GRANT EXECUTE ON FUNCTION core.promote_personal_dataset(TEXT) TO app_runtime;
    """)


def downgrade():
    raise RuntimeError("Do not discard business records or personal-ledger status")

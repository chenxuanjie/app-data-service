"""Constrain audit mappings to expenses in the same authorized dataset."""

from alembic import op

revision = "0002_import_record_scope"
down_revision = "0001_expense_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      DROP POLICY import_records_member ON core.import_records;
      CREATE POLICY import_records_member ON core.import_records TO app_runtime USING (
        EXISTS (
          SELECT 1 FROM core.import_batches b JOIN zheri.expenses e
          ON e.id = target_id AND e.dataset_id = b.dataset_id WHERE b.id = batch_id
        )
      ) WITH CHECK (
        EXISTS (
          SELECT 1 FROM core.import_batches b JOIN zheri.expenses e
          ON e.id = target_id AND e.dataset_id = b.dataset_id WHERE b.id = batch_id
        )
      );
    """)


def downgrade() -> None:
    op.execute("""
      DROP POLICY import_records_member ON core.import_records;
      CREATE POLICY import_records_member ON core.import_records TO app_runtime USING (
        EXISTS (SELECT 1 FROM core.import_batches b WHERE b.id = batch_id)
      ) WITH CHECK (
        EXISTS (SELECT 1 FROM core.import_batches b WHERE b.id = batch_id)
      );
    """)

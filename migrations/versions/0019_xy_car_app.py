"""Provision a namespace for the XY-CAR identity-only application."""

from alembic import op

revision = "0019_xy_car_app"
down_revision = "0018_release_batches"
branch_labels = depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA xy_car_app")
    op.execute("REVOKE ALL ON SCHEMA xy_car_app FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA xy_car_app TO app_runtime")


def downgrade():
    # Refuse to drop an in-use application or remove release/account records.
    connection = op.get_bind()
    if connection.exec_driver_sql("SELECT 1 FROM core.apps WHERE id = 'xy_car_app'").scalar():
        raise RuntimeError("Remove XY-CAR registration through a reviewed migration first")
    op.execute("DROP SCHEMA xy_car_app")

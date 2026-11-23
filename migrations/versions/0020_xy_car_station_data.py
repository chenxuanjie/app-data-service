"""Persist private device settings and named map missions for XY-CAR."""

from alembic import op

revision = "0020_xy_car_station_data"
down_revision = "0019_xy_car_app"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE xy_car_app.device_settings (
          user_id TEXT PRIMARY KEY REFERENCES core.users(id),
          name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
          host TEXT NOT NULL CHECK (char_length(host) BETWEEN 1 AND 253),
          port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535)
        );
        CREATE TABLE xy_car_app.missions (
          user_id TEXT NOT NULL REFERENCES core.users(id),
          name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
          waypoints JSONB NOT NULL CHECK (
            jsonb_typeof(waypoints) = 'array' AND jsonb_array_length(waypoints) <= 256
          ),
          PRIMARY KEY (user_id, name)
        );
    """)
    for table in ("device_settings", "missions"):
        op.execute(f"""
            ALTER TABLE xy_car_app.{table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE xy_car_app.{table} FORCE ROW LEVEL SECURITY;
            GRANT SELECT, INSERT, UPDATE ON xy_car_app.{table} TO app_runtime;
            CREATE POLICY own_station_data ON xy_car_app.{table} TO app_runtime
            USING (
              core.current_app_id() = 'xy_car_app'
              AND core.app_enabled() AND user_id = core.actor_id()
            )
            WITH CHECK (
              core.current_app_id() = 'xy_car_app'
              AND core.app_enabled() AND user_id = core.actor_id()
            );
        """)


def downgrade():
    raise RuntimeError("Export XY-CAR device settings and missions before removing these tables")

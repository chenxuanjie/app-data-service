"""Store a revisioned vehicle list for each XY-CAR account."""

from alembic import op

revision = "0021_xy_car_vehicle_connections"
down_revision = "0020_xy_car_station_data"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE xy_car_app.vehicle_connections (
          user_id TEXT PRIMARY KEY REFERENCES core.users(id),
          revision BIGINT NOT NULL CHECK (revision > 0),
          retired JSONB NOT NULL DEFAULT '[]'::JSONB CHECK (jsonb_typeof(retired) = 'array'),
          vehicles JSONB NOT NULL CHECK (
            jsonb_typeof(vehicles) = 'array' AND jsonb_array_length(vehicles) <= 64
          )
        );
        ALTER TABLE xy_car_app.device_settings NO FORCE ROW LEVEL SECURITY;
        WITH legacy AS (
          SELECT user_id, name,
                 'ws://' || CASE WHEN host LIKE '%:%' AND host NOT LIKE '[%'
                                 THEN '[' || host || ']' ELSE host END
                          || ':' || port || '/ws' AS endpoint,
                 port <> 10090 AND (
                   lower(trim(both '[]' from host)) ~
                     '^(127[.]|10[.]|192[.]168[.]|172[.]1[6-9][.]|172[.]2[0-9][.]|172[.]3[01][.]|::1$|fe80:|f[cd][0-9a-f]{2}:)'
                   OR lower(host) LIKE '%.local'
                   OR host NOT LIKE '%.%' AND host NOT LIKE '%:%'
                 ) AS is_local
            FROM xy_car_app.device_settings
        )
        INSERT INTO xy_car_app.vehicle_connections (user_id, revision, vehicles)
        SELECT user_id, 1, jsonb_build_array(jsonb_build_object(
          'id', md5(user_id), 'name', name, 'preference', 'auto',
          'lan', CASE WHEN is_local THEN endpoint END,
          'remote', CASE WHEN NOT is_local THEN endpoint END
        )) FROM legacy;
        ALTER TABLE xy_car_app.device_settings FORCE ROW LEVEL SECURITY;

        ALTER TABLE xy_car_app.vehicle_connections ENABLE ROW LEVEL SECURITY;
        ALTER TABLE xy_car_app.vehicle_connections FORCE ROW LEVEL SECURITY;
        GRANT SELECT, INSERT, UPDATE ON xy_car_app.vehicle_connections TO app_runtime;
        CREATE POLICY own_vehicle_connections ON xy_car_app.vehicle_connections TO app_runtime
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
    raise RuntimeError("Export XY-CAR vehicle lists before removing this table")

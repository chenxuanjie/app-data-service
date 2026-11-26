"""Station data is private to the signed-in XY-CAR account."""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from test_sessions import authenticated as authenticated

from app_data_service.api import create_app
from app_data_service.store import user_transaction

BASE = "/v1/apps/xy_car_app"


def sign_in(client, account, app="xy_car_app"):
    response = client.post(
        f"/v1/apps/{app}/auth/login",
        headers={"X-App-ID": app},
        json={"account": account, "password": "fixture-password"},
    )
    assert response.status_code == 200
    identity = response.json()
    return identity["user_id"], {
        "X-App-ID": "xy_car_app",
        "Authorization": "Bearer " + identity["access_token"],
    }


def test_station_data_round_trip_and_isolation(authenticated, db):
    client, *_ = authenticated
    owner_id, owner = sign_in(client, "alice")
    other_id, other = sign_in(client, "bob")
    _, wrong_app = sign_in(client, "alice", "board_game_agent")
    device = BASE + "/station/device"
    mission = BASE + "/station/mission"
    missions = BASE + "/station/missions"
    assert client.get(device, headers={"X-App-ID": "xy_car_app"}).status_code == 401
    assert client.get(device, headers=wrong_app).status_code == 401
    assert client.get(device, headers=owner).json() == {"device": None}

    configuration = {"name": "巡检车", "host": "192.0.2.5", "port": 9000}
    assert client.post(device, headers=owner, json=configuration).json() == {
        "device": configuration
    }
    assert client.get(device, headers=owner).headers["Cache-Control"] == "no-store"
    assert client.get(device, headers=owner).json() == {"device": configuration}
    assert client.get(device, headers=other).json() == {"device": None}
    assert (
        client.post(device, headers=owner, json={**configuration, "port": 65536}).status_code == 422
    )
    assert (
        client.post(device, headers=owner, json={**configuration, "user_id": other_id}).status_code
        == 422
    )

    payload = {"name": "巡检 & A", "waypoints": [{"x": 0.75, "y": 0}, {"x": 0.2, "y": 1}]}
    assert client.post(mission, headers=owner, json=payload).json() == {"mission": payload}
    assert client.post(mission, headers=owner, json=payload).status_code == 200
    assert client.get(missions, headers=owner).json() == {"names": [payload["name"]], "next": None}
    assert client.get(missions, headers=other).json() == {"names": [], "next": None}
    query = {"name": payload["name"]}
    assert client.get(mission, headers=owner, params=query).json() == {"mission": payload}
    assert client.get(mission, headers=other, params=query).status_code == 404
    bad = {**payload, "waypoints": [{"x": -0.01, "y": 0.25}]}
    assert client.post(mission, headers=owner, json=bad).status_code == 422
    assert (
        client.get(mission, headers=owner, params=query).json()["mission"]["waypoints"]
        == payload["waypoints"]
    )
    second = {"name": "第二组", "waypoints": []}
    assert client.post(mission, headers=owner, json=second).status_code == 200
    changed = {**payload, "waypoints": list(reversed(payload["waypoints"]))}
    assert client.post(mission, headers=owner, json=changed).status_code == 200
    assert client.get(mission, headers=owner, params=query).json() == {"mission": changed}
    assert set(client.get(missions, headers=owner).json()["names"]) == {
        payload["name"],
        second["name"],
    }

    with user_transaction(db.runtime, owner_id, app_id="board_game_agent") as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM xy_car_app.missions")) == 0
    with user_transaction(db.runtime, other_id, app_id="xy_car_app") as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM xy_car_app.device_settings")) == 0
    with pytest.raises(DBAPIError, match="row-level security"):
        with user_transaction(db.runtime, other_id, app_id="xy_car_app") as connection:
            connection.execute(
                sa.text("INSERT INTO xy_car_app.missions VALUES (:owner, 'forged', '[]')"),
                {"owner": owner_id},
            )


@pytest.fixture
def browser_station(authenticated, db):
    _, _, authority, manager = authenticated
    origin = "https://station.test"
    app = create_app(
        db.runtime,
        authority,
        "https://data.test/powersync",
        sessions=manager,
        application_settings={"xy_car_app": {"browser_origin": origin}},
    )
    with TestClient(
        app, base_url=origin, headers={"Origin": origin, "X-App-ID": "xy_car_app"}
    ) as browser:
        auth = BASE + "/auth/browser/"
        bootstrap = browser.post(auth + "bootstrap", json={})
        assert bootstrap.status_code == 200
        browser.headers["X-CSRF-Token"] = bootstrap.json()["csrf_token"]
        login = browser.post(
            auth + "login", json={"account": "alice", "password": "fixture-password"}
        )
        assert login.status_code == 200
        browser.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        yield browser, login.json()["user_id"]


def test_vehicle_list_revisions_and_deleted_ids_are_account_scoped(
    authenticated, browser_station, db
):
    client, *_ = authenticated
    web, owner_id = browser_station
    same_owner, native = sign_in(client, "alice")
    other_id, other = sign_in(client, "bob")
    assert same_owner == owner_id
    route = BASE + "/station/vehicles"
    initial = {"revision": 0, "vehicles": []}
    assert web.get(route).json() == initial
    assert client.get(route, headers=other).json() == initial

    first = {
        "id": "1" * 32,
        "name": "巡检车",
        "lan": "ws://192.0.2.5:9090/ws",
        "remote": "wss://station.example:8090/ws",
        "preference": "auto",
    }
    second = {**first, "id": "2" * 32, "name": "测试车", "remote": None}
    bad = {**first, "remote": "wss://user:secret@example.com/ws"}
    assert web.post(route, json={"revision": 0, "vehicles": [bad]}).status_code == 422
    assert web.post(route, json={"revision": 0, "vehicles": [first]}).json() == {
        "revision": 1,
        "vehicles": [first],
    }
    assert client.get(route, headers=native).json() == {"revision": 1, "vehicles": [first]}
    assert client.get(route, headers=other).json() == initial
    assert (
        client.post(route, headers=native, json={"revision": 0, "vehicles": [second]}).status_code
        == 409
    )

    both = [first, second]
    assert client.post(route, headers=native, json={"revision": 1, "vehicles": both}).json() == {
        "revision": 2,
        "vehicles": both,
    }
    assert web.post(route, json={"revision": 2, "vehicles": [second]}).json() == {
        "revision": 3,
        "vehicles": [second],
    }
    assert client.post(route, headers=native, json={"revision": 3, "vehicles": both}).json() == {
        "revision": 4,
        "vehicles": [second],
    }
    with user_transaction(db.runtime, other_id, app_id="xy_car_app") as connection:
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM xy_car_app.vehicle_connections")) == 0
        )


def test_vehicle_migration_copies_existing_station_settings(db):
    migration = importlib.import_module("migrations.versions.0021_xy_car_vehicle_connections")
    with db.migrator.connect() as connection:
        # Recreate the previous schema in a transaction rolled back when the connection closes.
        connection.exec_driver_sql("DROP TABLE xy_car_app.vehicle_connections")
        connection.exec_driver_sql(
            "ALTER TABLE xy_car_app.device_settings NO FORCE ROW LEVEL SECURITY"
        )
        connection.exec_driver_sql("""
            INSERT INTO xy_car_app.device_settings VALUES
              ('alice', 'LAN vehicle', 'fd00::10', 9090),
              ('bob', 'FRP vehicle', 'station.example', 10090)
        """)
        connection.exec_driver_sql(
            "ALTER TABLE xy_car_app.device_settings FORCE ROW LEVEL SECURITY"
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.exec_driver_sql(
            "ALTER TABLE xy_car_app.vehicle_connections NO FORCE ROW LEVEL SECURITY"
        )
        rows = connection.exec_driver_sql("""
            SELECT revision, vehicles -> 0 AS vehicle
            FROM xy_car_app.vehicle_connections
            WHERE user_id IN ('alice', 'bob')
            ORDER BY user_id
        """).all()
        assert [(revision, vehicle["lan"], vehicle["remote"]) for revision, vehicle in rows] == [
            (1, "ws://[fd00::10]:9090/ws", None),
            (1, None, "ws://station.example:10090/ws"),
        ]

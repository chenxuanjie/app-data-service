"""Station data is private to the signed-in XY-CAR account."""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_sessions import authenticated as authenticated

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

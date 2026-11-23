"""Authenticated device settings and ordered, normalized map waypoints."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.dialects.postgresql import JSONB, insert

from app_data_service.dependencies import Actor, application_router
from app_data_service.store import user_transaction

PREFIX = "/v1/apps/xy_car_app/station"
PAGE_SIZE = 50
Name = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$"
    ),
]
Coordinate = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]

device = sa.table(
    "device_settings",
    sa.column("user_id", sa.Text),
    sa.column("name", sa.Text),
    sa.column("host", sa.Text),
    sa.column("port", sa.Integer),
    schema="xy_car_app",
)
mission = sa.table(
    "missions",
    sa.column("user_id", sa.Text),
    sa.column("name", sa.Text),
    sa.column("waypoints", JSONB),
    schema="xy_car_app",
)


class DeviceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    host: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=253,
            pattern=r"^[^\s/?#@\\\x00-\x1f\x7f]+$",
        ),
    ]
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]


class Waypoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: Coordinate
    y: Coordinate


class Mission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    waypoints: Annotated[list[Waypoint], Field(max_length=256)]


def private_json(payload):
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def build_router(runtime):
    router = application_router("xy_car_app")

    @router.get(PREFIX + "/device")
    def read_device(actor: Actor):
        query = sa.select(device.c.name, device.c.host, device.c.port).where(
            device.c.user_id == actor.user_id
        )
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            result = connection.execute(query).mappings().one_or_none()
        return private_json({"device": dict(result) if result is not None else None})

    @router.post(PREFIX + "/device")
    def save_device(body: DeviceSettings, actor: Actor):
        values = body.model_dump()
        query = insert(device).values(user_id=actor.user_id, **values)
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            connection.execute(query.on_conflict_do_update(index_elements=["user_id"], set_=values))
        return private_json({"device": values})

    @router.get(PREFIX + "/missions")
    def list_missions(actor: Actor, after: Annotated[Name | None, Query()] = None):
        query = (
            sa.select(mission.c.name)
            .where(mission.c.user_id == actor.user_id)
            .order_by(mission.c.name)
            .limit(PAGE_SIZE + 1)
        )
        if after is not None:
            query = query.where(mission.c.name > after)
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            names = connection.execute(query).scalars().all()
        return private_json(
            {
                "names": names[:PAGE_SIZE],
                "next": names[PAGE_SIZE - 1] if len(names) > PAGE_SIZE else None,
            }
        )

    @router.get(PREFIX + "/mission")
    def read_mission(name: Annotated[Name, Query()], actor: Actor):
        query = sa.select(mission.c.name, mission.c.waypoints).where(
            mission.c.user_id == actor.user_id, mission.c.name == name
        )
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            result = connection.execute(query).mappings().one_or_none()
        if result is None:
            raise HTTPException(404, "mission_not_found")
        return private_json({"mission": dict(result)})

    @router.post(PREFIX + "/mission")
    def save_mission(body: Mission, actor: Actor):
        values = body.model_dump()
        query = insert(mission).values(user_id=actor.user_id, **values)
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as connection:
            connection.execute(
                query.on_conflict_do_update(
                    index_elements=["user_id", "name"],
                    set_={"waypoints": query.excluded.waypoints},
                )
            )
        return private_json({"mission": values})

    return router

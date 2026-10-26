"""Trusted installed modules, explicit application selection and deployment settings."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import sqlalchemy as sa
from fastapi import Header, HTTPException, Request


def application_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value):
        raise ValueError("Invalid application identifier")
    return value


@dataclass(frozen=True)
class ApplicationDefinition:
    id: str
    name: str
    schema: str
    personal_data: bool = True
    personal_space_name: str = "个人数据"
    personal_id_prefix: str | None = None
    file_areas: tuple[str, ...] = ()
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    sync_streams: Mapping[str, Any] = field(default_factory=dict)
    replication_tables: tuple[str, ...] = ()
    router_factory: Callable | None = None
    cli_factory: Callable | None = None

    def __post_init__(self):
        application_id(self.id)
        application_id(self.schema)
        if self.schema in {"core", "public", "pg_catalog", "information_schema"}:
            raise ValueError("Application business schema must be separate")
        if not self.name or not self.personal_space_name:
            raise ValueError("Application names are required")
        if self.personal_id_prefix is not None and not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", self.personal_id_prefix
        ):
            raise ValueError("Invalid personal-space prefix")
        if len(set(self.file_areas)) != len(self.file_areas) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", area) for area in self.file_areas
        ):
            raise ValueError("Invalid application file areas")
        json.dumps(dict(self.capabilities))
        json.dumps(dict(self.sync_streams))
        if any(
            not table.startswith(self.schema + ".")
            or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}\.[a-z][a-z0-9_]{0,62}", table)
            for table in self.replication_tables
        ):
            raise ValueError("Replicated business tables must belong to their module schema")
        if self.sync_streams and not self.replication_tables:
            raise ValueError("Sync modules must declare their publication tables")
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        object.__setattr__(self, "sync_streams", MappingProxyType(dict(self.sync_streams)))


class ApplicationRegistry:
    def __init__(self, definitions):
        definitions = tuple(definitions)
        self._definitions = MappingProxyType({d.id: d for d in definitions})
        if not definitions or len(self._definitions) != len(definitions):
            raise ValueError("Installed applications must have unique IDs")
        if len({d.schema for d in definitions}) != len(definitions):
            raise ValueError("Applications require separate business schemas")
        prefixes = [d.personal_id_prefix for d in definitions if d.personal_id_prefix is not None]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("Personal-space prefixes must be unique")

    @property
    def definitions(self):
        return tuple(self._definitions.values())

    def get(self, identifier):
        application_id(identifier)
        try:
            return self._definitions[identifier]
        except KeyError as error:
            raise ValueError("Application module is not installed") from error

    def validate_database(self, engine):
        """Check registrations; runtime never creates schemas or grants permissions."""
        from .models import App

        with engine.connect() as c:
            rows = {row["id"]: row for row in c.execute(sa.select(App)).mappings()}
            schemas = set(c.execute(sa.text("SELECT nspname FROM pg_namespace")).scalars())
            for d in self.definitions:
                row = rows.get(d.id)
                if row is None or (
                    row["name"],
                    row["schema_name"],
                    row["personal_data"],
                    row["personal_space_name"],
                    row["personal_id_prefix"],
                ) != (
                    d.name,
                    d.schema,
                    d.personal_data,
                    d.personal_space_name,
                    d.personal_id_prefix,
                ):
                    raise ValueError(f"Application registration requires migration: {d.id}")
                if d.schema not in schemas:
                    raise ValueError(f"Application schema requires migration: {d.id}")

    def ensure_enabled(self, connection, identifier):
        from .models import App

        self.get(identifier)
        if not connection.scalar(sa.select(App.enabled).where(App.id == identifier)):
            raise HTTPException(403, "application_disabled")

    def settings_from_environment(self, environment):
        if any(
            key.startswith("APP_DATA_")
            and key.endswith(("_BROWSER_ORIGIN", "_UPDATES_ROOT", "_RESOURCES_ROOT"))
            for key in environment
        ):
            raise ValueError(
                "Replace per-application environment variables with APP_DATA_APPLICATIONS"
            )
        raw = environment.get("APP_DATA_APPLICATIONS", "{}")
        if len(raw) > 64 * 1024:
            raise ValueError("Application configuration exceeds its budget")
        settings = json.loads(raw)
        if not isinstance(settings, dict):
            raise ValueError("Application settings must be an object")
        return self.validate_settings(settings)

    def validate_settings(self, settings):
        from .files import relative_path
        from .origins import browser_origin

        if not isinstance(settings, dict):
            raise ValueError("Application settings must be an object")
        result = {}
        for key, value in settings.items():
            definition = self.get(key)
            if not isinstance(value, dict) or set(value) - {"browser_origin", "file_roots"}:
                raise ValueError("Unknown application settings")
            roots = value.get("file_roots", {})
            if not isinstance(roots, dict) or set(roots) - set(definition.file_areas):
                raise ValueError("File areas must be declared by the application module")
            for root in roots.values():
                if not isinstance(root, str) or not root.startswith("/"):
                    raise ValueError("Explicit absolute application file roots required")
                try:
                    relative_path(root[1:])
                except HTTPException as error:
                    raise ValueError("Invalid application file root") from error
            result[key] = {**value, "file_roots": dict(roots)}
            if value.get("browser_origin") is not None:
                result[key]["browser_origin"] = browser_origin(value["browser_origin"])
        origins = [
            value["browser_origin"] for value in result.values() if value.get("browser_origin")
        ]
        if len(origins) != len(set(origins)):
            raise ValueError("Each application requires its own browser origin")
        return result

    def sync_config(self):
        streams = {}
        for definition in self.definitions:
            for key, value in definition.sync_streams.items():
                if key in streams:
                    raise ValueError("Sync stream names must be unique across installed modules")
                streams[key] = json.loads(json.dumps(value))
        return {"config": {"edition": 3, "storage_version": 2}, "streams": streams}

    @property
    def publication_tables(self):
        return sorted(
            {"core.datasets", "core.dataset_members"}
            | {table for definition in self.definitions for table in definition.replication_tables}
        )


def request_application(request: Request, x_app_id: str | None = Header(default=None)) -> str:
    registry = request.app.state.applications
    path_app = request.path_params.get("application_id")
    owner = getattr(request.scope.get("route"), "application_id", None)
    expected = path_app or owner
    if expected is not None and x_app_id is not None and expected != x_app_id:
        raise HTTPException(401 if owner else 400, "application_mismatch")
    identifier = expected or x_app_id
    try:
        definition = registry.get(identifier)
    except ValueError as error:
        raise HTTPException(400, "application_required") from error
    with request.app.state.engine.connect() as c:
        registry.ensure_enabled(c, definition.id)
    return definition.id

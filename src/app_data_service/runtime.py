"""Runtime services assembled once for the installed application modules."""

from dataclasses import dataclass, field
from typing import Any

from .applications import ApplicationRegistry


@dataclass
class ServiceRuntime:
    app: Any
    engine: Any
    authority: Any
    applications: ApplicationRegistry
    sync_endpoint: str
    sessions: Any = None
    platform_router: Any = None
    browsers: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    auth_quota: Any = None
    auth_capacity: Any = None
    application_settings: dict = field(default_factory=dict)

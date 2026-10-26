"""Verified request scope shared by application modules."""

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .applications import request_application

Application = Annotated[str, Depends(request_application)]
bearer = HTTPBearer(auto_error=False)
Credential = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


@dataclass(frozen=True)
class RequestScope:
    user_id: str
    app_id: str
    session_id: str | None


def identity(request: Request, credentials: Credential, app_id: Application) -> RequestScope:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    runtime = request.app.state.runtime
    try:
        claims = runtime.authority.claims(credentials.credentials, app_id=app_id)
        if runtime.sessions:
            runtime.sessions.session(claims["sub"], claims.get("session_id"), app_id)
        return RequestScope(claims["sub"], app_id, claims.get("session_id"))
    except jwt.InvalidTokenError as error:
        raise HTTPException(401, "Invalid or expired token") from error


Actor = Annotated[RequestScope, Depends(identity)]


def application_router(identifier: str) -> APIRouter:
    """A module route has a fixed owner, even without an application header."""

    class ApplicationRoute(APIRoute):
        application_id = identifier

    return APIRouter(route_class=ApplicationRoute, dependencies=[Depends(request_application)])

"""Shared request-scoped dependencies.

Everything long-lived (settings, policy bundle, scorer client, DB pool, session registry, validators)
is built once in the app lifespan and hung off ``app.state``. These helpers read it back. Nothing here
constructs a client per request — a per-request gRPC channel or JWKS fetch would show up as
first-window latency on every session.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings
from app.policy.loader import PolicyBundle
from app.scorer.client import ScorerClient
from app.security.jwt import AuthError, Principal, TokenValidator, bearer_from_header
from app.session_registry import SessionRegistry


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_policy(request: Request) -> PolicyBundle:
    return request.app.state.policy


def get_registry(request: Request) -> SessionRegistry:
    return request.app.state.registry


def get_scorer(request: Request) -> ScorerClient:
    return request.app.state.scorer


def get_validator(request: Request) -> TokenValidator:
    return request.app.state.token_validator


def get_audit(request: Request) -> Any:
    return request.app.state.audit


async def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Validate the bearer token and return the caller.

    Always ``401`` with a static body. It never reports which check failed, since that turns the
    endpoint into a token-validation oracle (rules.md R-17).
    """
    validator: TokenValidator = request.app.state.token_validator
    try:
        return await validator.validate(bearer_from_header(authorization))
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_INVALID", "message": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
CurrentSettings = Annotated[Settings, Depends(get_settings_dep)]
CurrentPolicy = Annotated[PolicyBundle, Depends(get_policy)]
CurrentRegistry = Annotated[SessionRegistry, Depends(get_registry)]
CurrentScorer = Annotated[ScorerClient, Depends(get_scorer)]
CurrentAudit = Annotated[Any, Depends(get_audit)]

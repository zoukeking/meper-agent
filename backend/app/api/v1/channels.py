"""Channel management API (admin) + inbound webhook receiver (public).

Two distinct auth modes:
- Management endpoints (/api/v1/channels/*): JWT (admin role), enforced at
  the router level via ``Depends(require_role(UserRole.ADMIN))``.
- Inbound webhook (/api/v1/channels/inbound/...): platform signature
  verification done by each adapter (no JWT — the IM platform doesn't carry
  our API key).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.channels.connections import get_connection_manager
from app.channels.registry import ChannelRegistry
from app.core.security import get_current_user, require_role
from app.models.channel import ChannelStatus
from app.models.user import UserRole
from app.schemas.channel import (
    PROVIDER_SCHEMAS,
    RECEIVE_MODE_LONG_CONNECTION,
    RECEIVE_MODES,
    ChannelCreateRequest,
    ChannelListResponse,
    ChannelResponse,
    ChannelUpdateRequest,
    ProviderSchema,
    ProviderSchemaResponse,
)
from app.services.channel_service import ChannelService

logger = logging.getLogger(__name__)


def _to_response(cfg, base_url: str) -> ChannelResponse:
    """Build a ChannelResponse with masked credentials + full inbound URL.

    ``base_url`` is the request's base URL (no trailing slash); for list
    responses where no request body is available, an empty base_url produces
    a relative inbound_url (acceptable for list previews).

    The inbound URL carries ``?secret=<webhook_secret>`` so the operator can
    paste it directly as the platform callback URL — the inbound receiver
    checks it as a second factor on top of each platform's signature
    verification.

    For long-connection channels the inbound URL is still emitted (it's
    harmless) but the live ``connection_status`` is what the UI surfaces.
    """
    prefix = f"{base_url}/api/v1/channels/inbound" if base_url else "/api/v1/channels/inbound"
    inbound_url = f"{prefix}/{cfg.provider}/{cfg.id}?secret={cfg.webhook_secret}"
    connection_status = get_connection_manager().connection_status(cfg.id)
    return ChannelResponse(
        id=cfg.id, name=cfg.name, provider=cfg.provider,
        agent_id=cfg.agent_id, owner_user_id=cfg.owner_user_id,
        enabled=cfg.enabled, status=cfg.status, receive_mode=cfg.receive_mode,
        credentials=ChannelService.mask_credentials(cfg.credentials),
        inbound_url=inbound_url,
        connection_status=connection_status,
        created_at=cfg.created_at, updated_at=cfg.updated_at,
    )


async def _reload(channel_id: str) -> None:
    """Notify the connection manager of a config change. Best-effort —
    webhook-mode channels are unaffected; failures here don't break the
    management request."""
    try:
        await get_connection_manager().reload_channel(channel_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("connection_reload_failed channel=%s err=%s", channel_id, exc)


def _validate_receive_mode(mode: str) -> str:
    if mode not in RECEIVE_MODES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"invalid receive_mode: {mode!r}, must be one of {RECEIVE_MODES}",
        )
    return mode


# ---------------------------------------------------------------------------
# Management router — JWT + admin role
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/channels",
    tags=["channels"],
    dependencies=[Depends(get_current_user), Depends(require_role(UserRole.ADMIN))],
)


@router.get("/providers/schema", response_model=ProviderSchemaResponse)
async def get_provider_schema() -> ProviderSchemaResponse:
    """Return provider credential field definitions + supported receive modes.

    ``receive_modes`` reflects *protocol* capability merged with the runtime
    availability of a ConnectionClient factory (e.g. wecom in first iteration
    has no factory → only webhook is offered even though the protocol supports
    long-connection) and the per-provider global enable flag in settings.
    """
    from app.core.config import settings

    mgr = get_connection_manager()
    # Map provider → long-connection enabled flag in settings
    enable_flags = {
        "lark": settings.CHANNEL_LARK_LONG_CONNECTION_ENABLED,
        "dingtalk": settings.CHANNEL_DINGTALK_LONG_CONNECTION_ENABLED,
        "wecom": settings.CHANNEL_WECOM_LONG_CONNECTION_ENABLED,
    }
    providers: dict[str, ProviderSchema] = {}
    for name, schema in PROVIDER_SCHEMAS.items():
        modes = list(schema.receive_modes)
        # Drop long_connection if no factory is registered AND the global
        # flag is off — don't offer a mode the server can't actually serve.
        if (
            RECEIVE_MODE_LONG_CONNECTION in modes
            and not (mgr.supports(name) and enable_flags.get(name, False))
        ):
            modes = [m for m in modes if m != RECEIVE_MODE_LONG_CONNECTION]
        providers[name] = schema.model_copy(update={"receive_modes": modes})
    return ProviderSchemaResponse(providers=providers)


@router.post("", response_model=ChannelResponse, status_code=201)
async def create_channel(
    body: ChannelCreateRequest,
    request: Request,
    user=Depends(get_current_user),
) -> ChannelResponse:
    _validate_receive_mode(body.receive_mode)
    cfg = await ChannelService.create_channel(
        name=body.name, provider=body.provider, agent_id=body.agent_id,
        credentials=body.credentials, owner_user_id=user.id,
        receive_mode=body.receive_mode,
    )
    await _reload(cfg.id)
    return _to_response(cfg, str(request.base_url).rstrip("/"))


@router.get("", response_model=ChannelListResponse)
async def list_channels(
    page: int = 1,
    page_size: int = 20,
    user=Depends(get_current_user),
) -> ChannelListResponse:
    items, total = await ChannelService.list_channels(
        owner_user_id=user.id, page=page, page_size=page_size,
    )
    return ChannelListResponse(
        items=[_to_response(c, "") for c in items],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{channel_id}", response_model=ChannelResponse)
async def get_channel(
    channel_id: str,
    request: Request,
    user=Depends(get_current_user),
) -> ChannelResponse:
    cfg = await ChannelService.get_channel(channel_id, user.id)
    if cfg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="channel not found")
    return _to_response(cfg, str(request.base_url).rstrip("/"))


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: str,
    body: ChannelUpdateRequest,
    request: Request,
    user=Depends(get_current_user),
) -> ChannelResponse:
    if body.receive_mode is not None:
        _validate_receive_mode(body.receive_mode)
    cfg = await ChannelService.update_channel(
        channel_id, user.id,
        name=body.name, agent_id=body.agent_id,
        credentials=body.credentials, enabled=body.enabled,
        receive_mode=body.receive_mode,
    )
    if cfg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="channel not found")
    await _reload(channel_id)
    return _to_response(cfg, str(request.base_url).rstrip("/"))


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(channel_id: str, user=Depends(get_current_user)) -> None:
    await ChannelService.delete_channel(channel_id)
    await _reload(channel_id)


@router.post("/{channel_id}/enable", status_code=200)
async def enable_channel(channel_id: str, user=Depends(get_current_user)) -> dict:
    await ChannelService.set_enabled(channel_id, True)
    await _reload(channel_id)
    return {"ok": True}


@router.post("/{channel_id}/disable", status_code=200)
async def disable_channel(channel_id: str, user=Depends(get_current_user)) -> dict:
    await ChannelService.set_enabled(channel_id, False)
    await _reload(channel_id)
    return {"ok": True}


@router.post("/{channel_id}/reset", status_code=200)
async def reset_channel(channel_id: str, user=Depends(get_current_user)) -> dict:
    await ChannelService.reset_degraded(channel_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Inbound webhook receiver — PUBLIC (no JWT). Auth = platform signature.
# ---------------------------------------------------------------------------

inbound_router = APIRouter(prefix="/channels/inbound", tags=["channels-inbound"])


@inbound_router.post("/{provider}/{channel_id}")
async def receive_inbound(provider: str, channel_id: str, request: Request):
    """Public endpoint receiving IM platform callbacks.

    Flow: load config → check enabled/degraded → adapter.verify_inbound →
    persist (dedup) event log → enqueue Celery task → ack. Lark URL
    verification challenges short-circuit before persistence.
    """
    from app.workers.tasks.channel_inbound import process_inbound

    cfg = await ChannelService.get_config(channel_id)
    if cfg is None or not cfg.enabled:
        return JSONResponse(
            {"error": "channel not found"}, status_code=status.HTTP_404_NOT_FOUND,
        )
    if cfg.status == ChannelStatus.DEGRADED:
        return JSONResponse(
            {"error": "channel degraded"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Second-factor auth: webhook_secret in the query string. This is a
    # defense against path-scanning forgery on top of each platform's own
    # signature verification (which some platforms' simple robots don't
    # provide at all). The secret is generated per channel at creation time.
    provided_secret = request.query_params.get("secret", "")
    if not provided_secret or provided_secret != cfg.webhook_secret:
        return JSONResponse(
            {"error": "invalid secret"}, status_code=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        # Adapters read the raw body via request._body (sync) so they can hash
        # it for signature verification. Pre-read it here to populate that
        # cache — verify_inbound itself is sync and can't await.
        await request.body()
        adapter = ChannelRegistry.get(provider)
    except KeyError:
        return JSONResponse(
            {"error": "unknown provider"}, status_code=status.HTTP_404_NOT_FOUND,
        )

    try:
        inbound = adapter.verify_inbound(request, cfg)
    except Exception as e:
        logger.warning("inbound verify failed (channel=%s): %s", channel_id, e)
        return JSONResponse(
            {"error": "verification failed"}, status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # None = special-case ack (e.g. Lark URL verification challenge).
    if inbound is None:
        challenge = getattr(request.state, "lark_challenge", None)
        if challenge:
            return JSONResponse({"challenge": challenge}, status_code=status.HTTP_200_OK)
        return JSONResponse({"ok": True}, status_code=status.HTTP_200_OK)

    log_id = await ChannelService.create_or_dedup_event(inbound)
    if log_id is None:
        # Duplicate of an already-processed event — ack and skip.
        return JSONResponse({"ok": True, "dedup": True}, status_code=status.HTTP_200_OK)

    process_inbound.delay(log_id)
    return JSONResponse({"ok": True}, status_code=status.HTTP_200_OK)

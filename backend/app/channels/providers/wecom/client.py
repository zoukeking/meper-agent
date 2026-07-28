"""WeCom message-sending client.

Docs: https://developer.work.weixin.qq.com/document/path/90236
Uses access_token + active send API.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.channels.errors import InvalidCredentialsError, SendFailedError
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

# corp_id -> (access_token, expires_at_epoch)
_token_cache: dict[str, tuple[str, float]] = {}


async def _get_access_token(config: ChannelConfig) -> str:
    corp_id_enc = config.credentials.get("corp_id")
    secret_enc = config.credentials.get("secret")
    if not corp_id_enc or not secret_enc:
        raise InvalidCredentialsError("wecom missing corp_id/secret")
    corp_id = decrypt_secret(corp_id_enc)
    secret = decrypt_secret(secret_enc)

    cached = _token_cache.get(corp_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                TOKEN_URL, params={"corpid": corp_id, "corpsecret": secret}
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise SendFailedError(f"wecom token http error: {e}") from e

    if data.get("errcode") != 0:
        raise InvalidCredentialsError(
            f"wecom token error: {data.get('errmsg')} ({data.get('errcode')})"
        )
    token = data["access_token"]
    _token_cache[corp_id] = (
        token,
        time.time() + data.get("expires_in", 7200),
    )
    return token


async def send_text_message(
    *, config: ChannelConfig, to_user: str, text: str
) -> str:
    """Send a text message via WeCom active-send API. Returns platform msg id."""
    token = await _get_access_token(config)
    agent_id = config.credentials.get("agent_id", "")
    payload = {
        "touser": to_user,
        "msgtype": "text",
        "agentid": agent_id,
        "text": {"content": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                SEND_URL, params={"access_token": token}, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise SendFailedError(f"wecom send http error: {e}") from e

    if data.get("errcode") != 0:
        raise SendFailedError(
            f"wecom send error: {data.get('errmsg')} ({data.get('errcode')})"
        )
    msgid = data.get("msgid")
    if not msgid:
        # WeCom may omit msgid in some responses; synthesize a stable fallback.
        msgid = f"wecom_{config.id}_{int(time.time() * 1000)}"
    return str(msgid)

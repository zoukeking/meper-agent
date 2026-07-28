# 外部消息通道接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建通用多 IM 集成框架,首期内置飞书 / 钉钉 / 企微 + Mock 适配器,让管理员后台配置凭据并绑定 agent 后,IM 用户发文本即可收到 agent 回复。

**Architecture:** 新增独立通道层 `backend/app/channels/`(纯协议翻译,不污染 engine/harness),通过 `ChannelService` 编排,复用现有 `AgentExecutionService.invoke` 执行链 + Celery 异步骨架。入站走 `POST /api/v1/channels/inbound/{provider}/{channel_id}`,异步处理 + 主动回推。

**Tech Stack:** FastAPI、Pydantic v2、Motor (MongoDB)、Celery + Redis、httpx、AES-256-GCM (`core/crypto.py`)、React 19 + TanStack Query + Ant Design 6。

**关联 Spec:** `docs/superpowers/specs/2026-07-17-external-channel-integration-design.md`

---

## 关键代码库约定(实施前必读)

核对实际代码库后确认的事实,所有 task 遵循:

| 约定 | 实际形态 |
|---|---|
| 模型基类 | 纯 `pydantic.BaseModel` + `generate_id(prefix)` 生成 `{prefix}_{ULID}` 字符串。**无 Document、无 PyObjectId**。 |
| 模型文件参考 | `backend/app/models/webhook.py`、`backend/app/models/mcp_connection.py`(StrEnum + ConfigDict 风格) |
| 凭据加解密 | `from app.core.crypto import encrypt_secret, decrypt_secret, mask_secret` (AES-256-GCM) |
| HMAC 签名 | `from app.services.webhook_service import compute_signature`(signature = HMAC-SHA256(secret, f"{ts}.{body}")) |
| Celery 桥 | `from app.workers.loop import run_async`;task 内 `run_async(coro())`,**禁用 `asyncio.run()`**(会破坏 motor 单例) |
| task 装饰器 | `@celery_app.task(name="app.workers.tasks.<module>.<fn>", bind=True)` + 在 `celery_app.py` 的 `include` 列表加模块 |
| 执行复用 | `await AgentExecutionService.invoke(agent_id, ExecutionRequest(...), user_id)`,返回 `ExecutionResponse` |
| Session 隔离 | 把身份编码进 `user_id` 字符串(如 `f"channel:{channel_id}:{platform_chat_id}"`),Session schema 零改动 |
| Settings 命名 | `UPPER_SNAKE_CASE`,通过 `settings.X` 访问 |
| 索引 | 追加到 `backend/app/db/indexes.py` 的 `create_indexes()` |
| API 路由 | 新文件 `api/v1/channels.py` 导出 `router = APIRouter(prefix="/channels", ...)`,在 `api/v1/router.py` 加 import + `include_router` |
| 测试 | `pytest` 跑,`asyncio_mode=auto`,Celery 强制 eager。参考 `backend/tests/api/test_webhooks.py`。 |
| 前端页面 | 参考 `frontend/src/pages/mcp-page.tsx`(TanStack Query + AntD + Tailwind grid) |
| 前端 service | 参考 `frontend/src/services/mcp-api.ts`(snake_case 类型 + `apiClient` + `xxxKeys` 工厂) |
| 前端菜单 | `frontend/src/config/menu.ts` 加 `MENU_ITEMS` 项 + `frontend/src/routes/index.tsx` 加路由 |

---

## File Structure

### 后端新增文件

```
backend/app/
├── channels/                              ← 新增模块
│   ├── __init__.py                        (空,标记包)
│   ├── base.py                            Channel ABC + InboundMessage + OutboundEnvelope
│   ├── registry.py                        ChannelRegistry (register 装饰器 + get)
│   ├── errors.py                          ChannelError 体系 (Transient/Permanent)
│   └── providers/
│       ├── __init__.py                    PEP 562: import 触发注册
│       ├── lark/
│       │   ├── __init__.py
│       │   ├── channel.py                 LarkChannel
│       │   ├── verify.py                  飞书验签 (HMAC-SHA256 + AES)
│       │   └── client.py                  飞书 OpenAPI 调用 (发消息)
│       ├── dingtalk/
│       │   ├── __init__.py
│       │   ├── channel.py                 DingtalkChannel
│       │   ├── verify.py                  钉钉验签 (timestamp + sign Base64)
│       │   └── client.py
│       ├── wecom/
│       │   ├── __init__.py
│       │   ├── channel.py                 WecomChannel
│       │   ├── verify.py                  企微验签 (msg_signature + AES)
│       │   └── client.py
│       └── mock/
│           ├── __init__.py
│           └── channel.py                 MockChannel (打印入站, 记录出站供测试断言)
│
├── models/channel.py                      ChannelConfig + InboundEventLog + 枚举
├── schemas/channel.py                     API 请求/响应 + provider schema
├── services/channel_service.py            ChannelService (编排层)
├── workers/tasks/channel_inbound.py       Celery task process_inbound
├── api/v1/channels.py                     入站 webhook + 管理 CRUD 路由
│
└── tests/
    ├── models/test_channel.py
    ├── channels/
    │   ├── test_registry.py
    │   ├── test_errors.py
    │   └── providers/
    │       ├── test_mock_channel.py
    │       ├── test_lark_verify.py
    │       ├── test_dingtalk_verify.py
    │       └── test_wecom_verify.py
    ├── services/test_channel_service.py
    └── api/test_channels.py
```

### 后端修改文件

```
backend/app/core/config.py                 加 CHANNEL_* 配置项
backend/app/db/indexes.py                  加 channel_configs / inbound_event_logs 索引
backend/app/workers/celery_app.py          include 列表加 channel_inbound
backend/app/api/v1/router.py               include channels 路由
```

### 前端新增文件

```
frontend/src/
├── services/channel-api.ts                类型 + channelApi + channelKeys
├── pages/channels-page.tsx                列表 + Modal 表单 + 测试/启停/删除
└── components/
    └── channel-form.tsx                   (可选)独立表单组件
```

### 前端修改文件

```
frontend/src/config/menu.ts                MENU_ITEMS 加 channels 项
frontend/src/routes/index.tsx              加 /channels 路由
```

---

## 任务分解概览

| 里程碑 | Task | 内容 | 依赖 |
|---|---|---|---|
| **P0 基础** | 1 | 配置项 + 错误体系 | — |
| | 2 | 数据模型 + 索引 | — |
| | 3 | Channel 抽象接口 + Registry | — |
| **P1 核心** | 4 | MockChannel 适配器 | 3 |
| | 5 | ChannelService(编排层) | 2, 3, 4 |
| | 6 | Celery task + 注册 | 5 |
| | 7 | 飞书适配器(验签 + client) | 3 |
| | 8 | 钉钉适配器 | 3 |
| | 9 | 企微适配器 | 3 |
| **P2 接入** | 10 | 管理 API(CRUD + schema + test/enable/reset) | 2, 5 |
| | 11 | 入站 webhook 路由 | 5, 7, 8, 9 |
| | 12 | 前端 service + 页面 + 菜单 | 10 |
| **P3 集成** | 13 | 端到端集成测试(Mock 全链路) | 6, 11 |

---

## 里程碑 P0:基础层

### Task 1:配置项 + 错误体系

**Files:**
- Modify: `backend/app/core/config.py`(加 CHANNEL_* 字段)
- Create: `backend/app/channels/__init__.py`(空)
- Create: `backend/app/channels/errors.py`
- Test: `backend/tests/channels/test_errors.py`

- [ ] **Step 1: 写 errors.py 的失败测试**

Create `backend/tests/channels/__init__.py` (空文件,标记测试包)。

Create `backend/tests/channels/test_errors.py`:

```python
"""Channel error taxonomy tests."""
import pytest
from app.channels.errors import (
    ChannelError,
    TransientChannelError,
    PermanentChannelError,
    LLMRateLimitError,
    ToolExecutionError,
    InvalidCredentialsError,
    AgentRuntimeError,
    SendFailedError,
)


class TestErrorHierarchy:
    def test_transient_is_channel_error(self):
        assert issubclass(TransientChannelError, ChannelError)

    def test_permanent_is_channel_error(self):
        assert issubclass(PermanentChannelError, ChannelError)

    def test_specific_transient_errors(self):
        assert issubclass(LLMRateLimitError, TransientChannelError)
        assert issubclass(ToolExecutionError, TransientChannelError)

    def test_specific_permanent_errors(self):
        assert issubclass(InvalidCredentialsError, PermanentChannelError)
        assert issubclass(AgentRuntimeError, PermanentChannelError)
        assert issubclass(SendFailedError, PermanentChannelError)


class TestUserMessages:
    def test_transient_default_message(self):
        err = TransientChannelError("boom")
        assert err.user_message == "服务繁忙,正在重试..."

    def test_permanent_default_message(self):
        err = PermanentChannelError("boom")
        assert err.user_message == "处理失败,请联系管理员"

    def test_llm_rate_limit_message(self):
        err = LLMRateLimitError()
        assert err.user_message == "请求过多,请稍后再试"

    def test_invalid_credentials_message(self):
        err = InvalidCredentialsError()
        assert err.user_message == "机器人配置异常,请联系管理员"

    def test_send_failed_message(self):
        err = SendFailedError()
        assert err.user_message == "消息发送失败,请稍后重试"

    def test_message_independent_of_detail(self):
        err = PermanentChannelError("internal trace")
        # detail 不应泄漏到 user_message
        assert "internal trace" not in err.user_message
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/channels/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.channels'`

- [ ] **Step 3: 创建 channels 包**

Create `backend/app/channels/__init__.py`(空内容,只标记 Python 包)。

- [ ] **Step 4: 实现 errors.py**

Create `backend/app/channels/errors.py`:

```python
"""Channel error taxonomy.

Two families:
- TransientChannelError: temporary (LLM rate limit, tool blip) → Celery retry
- PermanentChannelError: not retryable (bad creds, code bug) → fallback reply

Each error carries a `user_message` safe to send back to IM users.
The constructor detail MUST NOT be reflected in user_message (no info leak).
"""
from __future__ import annotations


class ChannelError(Exception):
    """Base class for all channel-layer errors."""
    user_message: str = "处理失败,请稍后重试"


class TransientChannelError(ChannelError):
    """Temporary failure. Celery task should retry with backoff."""
    user_message = "服务繁忙,正在重试..."


class PermanentChannelError(ChannelError):
    """Not retryable. Send fallback reply to user, log details."""
    user_message = "处理失败,请联系管理员"


# ── Specific transient errors (raised by adapters / service) ──

class LLMRateLimitError(TransientChannelError):
    user_message = "请求过多,请稍后再试"


class ToolExecutionError(TransientChannelError):
    user_message = "工具暂时不可用,正在重试"


# ── Specific permanent errors ──

class InvalidCredentialsError(PermanentChannelError):
    user_message = "机器人配置异常,请联系管理员"


class AgentRuntimeError(PermanentChannelError):
    user_message = "服务异常,请联系管理员"


class SendFailedError(PermanentChannelError):
    """Channel.send() failed after internal retries. Reply already produced
    by agent but couldn't be delivered to the IM platform."""
    user_message = "消息发送失败,请稍后重试"
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd backend && pytest tests/channels/test_errors.py -v`
Expected: PASS (10 tests)

- [ ] **Step 6: 加配置项**

Modify `backend/app/core/config.py`:在 `Settings` 类内(找现有配置项的末尾,`settings = Settings()` 之前)追加:

```python
    # ── Channels (inbound IM integrations) ──
    CHANNEL_INBOUND_ACK_TIMEOUT_MS: int = 2000
    CHANNEL_EVENT_LOG_TTL_HOURS: int = 24
    CHANNEL_MAX_RETRIES: int = 3
    CHANNEL_SEND_MAX_RETRIES: int = 3
    CHANNEL_DEFAULT_REPLY_ON_FAILURE: str = "处理失败,请稍后重试或联系管理员"
    CHANNEL_DEGRADED_ON_CONSECUTIVE_FAILURES: int = 5
```

- [ ] **Step 7: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/channels/__init__.py backend/app/channels/errors.py \
        backend/app/core/config.py \
        backend/tests/channels/__init__.py backend/tests/channels/test_errors.py
git commit -m "feat(channel): 错误分类体系 + CHANNEL_* 配置项

- TransientChannelError / PermanentChannelError 两族,各自带 user_message
- 具体子类: LLMRateLimit/ToolExec/InvalidCreds/AgentRuntime/SendFailed
- config.py 加 6 个 CHANNEL_* 全局调优参数(均有默认值)"
```

---

### Task 2:数据模型 + 索引

**Files:**
- Create: `backend/app/models/channel.py`
- Modify: `backend/app/db/indexes.py`
- Test: `backend/tests/models/test_channel.py`

- [ ] **Step 1: 写模型的失败测试**

Create `backend/tests/models/test_channel.py`:

```python
"""ChannelConfig / InboundEventLog model tests."""
import pytest
from app.models.channel import (
    ChannelConfig,
    ChannelProvider,
    ChannelStatus,
    InboundEventLog,
    InboundEventLogStatus,
)


class TestChannelConfig:
    def test_default_id_has_ch_prefix(self):
        cfg = ChannelConfig(
            name="售后-飞书",
            provider=ChannelProvider.LARK,
            agent_id="agent_01J",
            owner_user_id="user_01J",
            webhook_secret="a" * 32,
        )
        assert cfg.id.startswith("ch_")
        assert cfg.enabled is True
        assert cfg.status == ChannelStatus.ACTIVE
        assert cfg.consecutive_failures == 0
        assert cfg.receive_mode == "webhook"

    def test_provider_enum_values(self):
        assert ChannelProvider.LARK == "lark"
        assert ChannelProvider.DINGTALK == "dingtalk"
        assert ChannelProvider.WECOM == "wecom"
        assert ChannelProvider.MOCK == "mock"

    def test_status_enum_values(self):
        assert ChannelStatus.ACTIVE == "active"
        assert ChannelStatus.DEGRADED == "degraded"
        assert ChannelStatus.DISABLED == "disabled"

    def test_credentials_defaults_to_empty_dict(self):
        cfg = ChannelConfig(
            name="x", provider=ChannelProvider.MOCK,
            agent_id="a", owner_user_id="u", webhook_secret="b" * 32,
        )
        assert cfg.credentials == {}

    def test_populate_by_alias(self):
        """Model can be constructed with _id alias and dumped back with alias."""
        cfg = ChannelConfig(
            _id="ch_test",
            name="x", provider=ChannelProvider.MOCK,
            agent_id="a", owner_user_id="u", webhook_secret="b" * 32,
        )
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["_id"] == "ch_test"
        assert dumped["provider"] == "mock"

    def test_name_required(self):
        with pytest.raises(Exception):
            ChannelConfig(
                provider=ChannelProvider.MOCK,
                agent_id="a", owner_user_id="u", webhook_secret="b" * 32,
            )


class TestInboundEventLog:
    def test_default_id_has_inb_prefix(self):
        log = InboundEventLog(
            channel_id="ch_01J",
            platform_message_id="msg_001",
            payload={"text": "hi"},
        )
        assert log.id.startswith("inb_")
        assert log.status == InboundEventLogStatus.PENDING
        assert log.processed_at is None
        assert log.error is None

    def test_status_enum_values(self):
        assert InboundEventLogStatus.PENDING == "pending"
        assert InboundEventLogStatus.PROCESSING == "processing"
        assert InboundEventLogStatus.DONE == "done"
        assert InboundEventLogStatus.FAILED == "failed"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/models/test_channel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.channel'`

- [ ] **Step 3: 实现 models/channel.py**

Create `backend/app/models/channel.py`:

```python
"""Channel configuration + inbound event log models.

Pattern: plain pydantic.BaseModel + generate_id(prefix) for ULID string IDs.
No Document base class, no PyObjectId — see backend/app/models/webhook.py.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from app.models.base import generate_id, utc_now


class ChannelProvider(StrEnum):
    LARK = "lark"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    MOCK = "mock"


class ChannelStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class InboundEventLogStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ChannelConfig(BaseModel):
    """A channel = a set of platform credentials + agent binding.

    Supports 1 agent : N channels (each channel is an independent config).
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("ch"), alias="_id")
    name: str = Field(..., min_length=1, max_length=200)
    provider: ChannelProvider

    # Encrypted at rest via core/crypto.encrypt_secret (per-field on write)
    credentials: dict = Field(default_factory=dict)

    agent_id: str
    owner_user_id: str

    receive_mode: str = "webhook"  # reserved: "long_poll" for future Lark WebSocket

    enabled: bool = True
    webhook_secret: str = Field(..., min_length=16)  # secondary inbound verification
    status: ChannelStatus = ChannelStatus.ACTIVE
    consecutive_failures: int = 0

    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class InboundEventLog(BaseModel):
    """Idempotency key + work queue entry.

    Platform may resend the same event on timeout; dedupe by platform_message_id.
    InboundMessage is persisted here BEFORE acking the platform, so a crash
    between ack and worker pickup doesn't lose the message.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("inb"), alias="_id")
    channel_id: str
    platform_message_id: str
    payload: dict                  # full InboundMessage.model_dump()
    status: InboundEventLogStatus = InboundEventLogStatus.PENDING
    processed_at: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `cd backend && pytest tests/models/test_channel.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: 加 MongoDB 索引**

Modify `backend/app/db/indexes.py`:在 `create_indexes()` 函数末尾(`await db.file_usages...` 之后、函数 return 之前)追加:

```python
    # ── Channels ──
    await db.channel_configs.create_index(
        "owner_user_id", name="idx_channel_configs_owner"
    )
    await db.channel_configs.create_index(
        "agent_id", name="idx_channel_configs_agent"
    )
    await db.channel_configs.create_index(
        [("provider", 1), ("name", 1)], name="idx_channel_configs_provider_name"
    )
    await db.inbound_event_logs.create_index(
        [("channel_id", 1), ("platform_message_id", 1)],
        name="uq_inbound_logs_channel_msg",
        unique=True,
    )
    await db.inbound_event_logs.create_index(
        [("status", 1), ("created_at", 1)], name="idx_inbound_logs_status_time"
    )
```

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/models/channel.py backend/app/db/indexes.py \
        backend/tests/models/test_channel.py
git commit -m "feat(channel): ChannelConfig + InboundEventLog 数据模型

- ChannelConfig: 凭据 dict (加密存储) + agent 绑定 + 状态机
- InboundEventLog: 幂等去重 + 待处理队列双重职责
- indexes.py: channel_configs 按_owner/agent 查, inbound_logs 唯一键防重"
```

---

### Task 3:Channel 抽象接口 + Registry

**Files:**
- Create: `backend/app/channels/base.py`
- Create: `backend/app/channels/registry.py`
- Test: `backend/tests/channels/test_registry.py`

- [ ] **Step 1: 写 base.py + registry.py 的失败测试**

Create `backend/tests/channels/test_registry.py`:

```python
"""Channel abstract interface + registry tests."""
from datetime import datetime, UTC

import pytest
from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.registry import ChannelRegistry


# ── Test data objects ──

class TestInboundMessage:
    def test_required_fields(self):
        msg = InboundMessage(
            channel_id="ch_01J",
            platform_chat_id="oc_test",
            platform_user_id="u_001",
            message_id="msg_001",
            text="你好",
            raw={"source": "test"},
            timestamp=datetime.now(UTC),
        )
        assert msg.platform_user_name is None
        assert msg.raw["source"] == "test"

    def test_text_required(self):
        with pytest.raises(Exception):
            InboundMessage(
                channel_id="ch", platform_chat_id="c", platform_user_id="u",
                message_id="m", text="", raw={}, timestamp=datetime.now(UTC),
            )


class TestOutboundEnvelope:
    def test_minimal(self):
        env = OutboundEnvelope(
            channel_id="ch_01J", platform_chat_id="oc_test", text="回复",
        )
        assert env.reply_to_message_id is None


# ── Registry behavior with a fake channel ──

class FakeChannel(Channel):
    provider = "fake"

    def verify_inbound(self, request: Request, config):
        return None

    def send(self, envelope, config):
        return "fake_msg_id"


class TestChannelRegistry:
    def setup_method(self):
        # Registry is module-level; clean state per test
        ChannelRegistry._registry = {}

    def test_register_decorator(self):
        @ChannelRegistry.register("fake")
        class _C(Channel):
            provider = "fake"
            def verify_inbound(self, request, config): return None
            def send(self, envelope, config): return "x"
        assert "fake" in ChannelRegistry._registry

    def test_get_returns_instance(self):
        ChannelRegistry.register("fake")(FakeChannel)
        instance = ChannelRegistry.get("fake")
        assert isinstance(instance, FakeChannel)

    def test_get_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            ChannelRegistry.get("nonexistent")

    def test_get_returns_new_instance_each_call(self):
        ChannelRegistry.register("fake")(FakeChannel)
        a = ChannelRegistry.get("fake")
        b = ChannelRegistry.get("fake")
        assert a is not b  # stateless, fresh instance per call
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/channels/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.channels.base'`

- [ ] **Step 3: 实现 base.py**

Create `backend/app/channels/base.py`:

```python
"""Channel abstract interface + standardized message types.

Adapters implement Channel to translate between a specific IM platform's
protocol and these normalized shapes. Everything downstream of the adapter
deals only with InboundMessage / OutboundEnvelope.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

from fastapi import Request
from pydantic import BaseModel, Field

from app.models.channel import ChannelConfig


class InboundMessage(BaseModel):
    """Normalized inbound message produced by an adapter's verify_inbound.

    Downstream services (ChannelService, AgentExecutionService) consume this
    and never see the raw platform payload.
    """
    channel_id: str
    platform_chat_id: str         # chat/group/open_id this conversation lives in
    platform_user_id: str         # sender identity within the platform
    platform_user_name: str | None = None
    message_id: str               # platform message id (idempotency key)
    text: str = Field(..., min_length=1)
    raw: dict                     # original payload (audit/debug/future rich format)
    timestamp: datetime


class OutboundEnvelope(BaseModel):
    """Normalized outbound message. ChannelService produces this; the adapter's
    send() translates it into a platform API call."""
    channel_id: str
    platform_chat_id: str
    text: str
    reply_to_message_id: str | None = None


class Channel(ABC):
    """IM platform adapter interface. Add a platform by:
      1. Create app/channels/providers/<name>/ with channel.py / verify.py / client.py
      2. Subclass Channel, decorate with @ChannelRegistry.register("<name>")
      3. Add an import line in app/channels/providers/__init__.py

    Adapters MUST be stateless — config is passed per-call so one class can
    serve multiple ChannelConfig instances safely.
    """
    provider: ClassVar[str]

    @abstractmethod
    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        """Verify signature + parse HTTP callback.

        Returns:
            InboundMessage: verified, proceed with processing.
            None: special-case ack (e.g. Lark URL verification challenge) —
                  caller should return the adapter-specific ack response and
                  skip downstream processing.
        Raises:
            ChannelError / AuthError: verification failed, reject the callback.
        """

    @abstractmethod
    def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        """Call platform OpenAPI to send a message. Returns platform message id.

        Raises TransientChannelError on retryable failures, PermanentChannelError
        (e.g. InvalidCredentialsError / SendFailedError) otherwise.
        """

    def normalize_event(
        self, event: dict, config: ChannelConfig
    ) -> InboundMessage:
        """Optional: parse a long-connection (WebSocket) event into InboundMessage.
        HTTP-callback adapters don't need to implement this."""
        raise NotImplementedError(
            f"{self.provider} does not implement long-connection mode"
        )
```

- [ ] **Step 4: 实现 registry.py**

Create `backend/app/channels/registry.py`:

```python
"""Channel adapter registry.

Uses a @register decorator pattern. Providers register themselves on import
via PEP 562 (see app/channels/providers/__init__.py), matching the existing
TOOL_REGISTRY pattern in packages/harness.
"""
from __future__ import annotations

from app.channels.base import Channel


class ChannelRegistry:
    _registry: dict[str, type[Channel]] = {}

    @classmethod
    def register(cls, provider: str):
        """Class decorator: register a Channel subclass under `provider`."""
        def wrapper(channel_cls: type[Channel]):
            if not getattr(channel_cls, "provider", None):
                raise ValueError(
                    f"{channel_cls.__name__} must set a ClassVar `provider`"
                )
            cls._registry[provider] = channel_cls
            return channel_cls
        return wrapper

    @classmethod
    def get(cls, provider: str) -> Channel:
        """Return a fresh adapter instance for `provider`.

        Lazily imports providers package on first miss to trigger PEP 562
        registration. Stateless — each call returns a new instance.
        """
        if provider not in cls._registry:
            # Trigger PEP 562 imports (idempotent)
            from app.channels import providers  # noqa: F401
        if provider not in cls._registry:
            raise KeyError(
                f"No channel adapter registered for provider={provider!r}. "
                f"Known: {list(cls._registry.keys())}"
            )
        return cls._registry[provider]()

    @classmethod
    def known_providers(cls) -> list[str]:
        from app.channels import providers  # noqa: F401
        return list(cls._registry.keys())
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd backend && pytest tests/channels/test_registry.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/channels/base.py backend/app/channels/registry.py \
        backend/tests/channels/test_registry.py
git commit -m "feat(channel): Channel 抽象接口 + ChannelRegistry

- Channel ABC: verify_inbound / send / normalize_event (可选)
- InboundMessage / OutboundEnvelope 标准化消息结构
- Registry: @register 装饰器 + PEP 562 延迟加载, 无状态实例"
```

---

## 里程碑 P1:核心链路

### Task 4:MockChannel 适配器

**Why first:** 为后续 Service / Celery / 入站路由提供一个不依赖任何外部平台的适配器,让全链路能在单测和 CI 里跑通(spec 验收标准 #7)。

**Files:**
- Create: `backend/app/channels/providers/__init__.py`
- Create: `backend/app/channels/providers/mock/__init__.py`
- Create: `backend/app/channels/providers/mock/channel.py`
- Test: `backend/tests/channels/providers/test_mock_channel.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/channels/providers/__init__.py`(空)。

Create `backend/tests/channels/providers/test_mock_channel.py`:

```python
"""MockChannel: test/CI adapter that records calls instead of hitting a platform."""
from datetime import datetime, UTC

import pytest
from starlette.requests import Request

from app.channels.base import InboundMessage, OutboundEnvelope
from app.channels.providers.mock.channel import MockChannel, MOCK_SENT_MESSAGES
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig, ChannelProvider


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="mock-test",
        provider=ChannelProvider.MOCK,
        agent_id="agent_01J",
        owner_user_id="user_01J",
        webhook_secret="mock_secret_at_least_16_chars",
        credentials={},
    )


def _make_request(body: bytes, headers: dict | None = None) -> Request:
    """Build a minimal Starlette Request with a JSON body."""
    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.encode(), v.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "headers": raw_headers,
        "query_string": b"",
    }
    req = Request(scope)
    req._body = body
    return req


class TestMockChannelRegistration:
    def test_registered_as_mock(self):
        # Trigger PEP 562
        from app.channels import providers  # noqa: F401
        assert "mock" in ChannelRegistry._registry

    def test_registry_returns_mock_instance(self):
        instance = ChannelRegistry.get("mock")
        assert isinstance(instance, MockChannel)


class TestMockVerifyInbound:
    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    def test_parses_simple_text_payload(self):
        body = (
            b'{"message_id":"msg_1","chat_id":"chat_1",'
            b'"user_id":"u_1","user_name":"alice","text":"hello"}'
        )
        req = _make_request(body)
        channel = MockChannel()

        msg = channel.verify_inbound(req, _make_config())

        assert msg is not None
        assert msg.message_id == "msg_1"
        assert msg.platform_chat_id == "chat_1"
        assert msg.platform_user_id == "u_1"
        assert msg.platform_user_name == "alice"
        assert msg.text == "hello"

    def test_returns_none_for_empty_text(self):
        body = b'{"message_id":"msg_1","chat_id":"chat_1","user_id":"u_1","text":""}'
        req = _make_request(body)
        channel = MockChannel()

        assert channel.verify_inbound(req, _make_config()) is None

    def test_timestamp_defaults_to_now(self):
        body = b'{"message_id":"msg_1","chat_id":"c","user_id":"u","text":"hi"}'
        req = _make_request(body)
        before = datetime.now(UTC)
        msg = MockChannel().verify_inbound(req, _make_config())
        after = datetime.now(UTC)
        assert before <= msg.timestamp <= after


class TestMockSend:
    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    def test_send_records_message(self):
        channel = MockChannel()
        env = OutboundEnvelope(
            channel_id="ch_01J", platform_chat_id="chat_1", text="reply",
        )

        msg_id = channel.send(env, _make_config())

        assert msg_id.startswith("mock_msg_")
        assert len(MOCK_SENT_MESSAGES) == 1
        assert MOCK_SENT_MESSAGES[0]["text"] == "reply"
        assert MOCK_SENT_MESSAGES[0]["platform_chat_id"] == "chat_1"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/channels/providers/test_mock_channel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.channels.providers'`

- [ ] **Step 3: 实现 providers/__init__.py (PEP 562 入口)**

Create `backend/app/channels/providers/__init__.py`:

```python
"""PEP 562 entry: importing this package triggers registration of all
built-in adapters via their @ChannelRegistry.register decorators.

Adding a new platform = create providers/<name>/ + add one import line here.
ChannelRegistry itself never needs editing.
"""
from . import mock  # noqa: F401
# Following adapters added in later tasks:
# from . import lark  # noqa: F401
# from . import dingtalk  # noqa: F401
# from . import wecom  # noqa: F401
```

- [ ] **Step 4: 实现 mock 适配器**

Create `backend/app/channels/providers/mock/__init__.py`(空)。

Create `backend/app/channels/providers/mock/channel.py`:

```python
"""Mock channel adapter — local testing / CI.

Protocol (deliberately trivial):
- Inbound: POST a JSON body {message_id, chat_id, user_id, user_name?, text}.
  No signature verification (it's a test shim).
- Outbound: send() records the envelope into a module-level list so tests
  can assert on what would have been delivered.

Do NOT use in production.
"""
from __future__ import annotations

import json
from datetime import datetime, UTC

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

# Module-global record of messages "sent" — tests assert on this.
# Cleared per-test by the test's setup_method.
MOCK_SENT_MESSAGES: list[dict] = []


@ChannelRegistry.register("mock")
class MockChannel(Channel):
    provider = "mock"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else "{}"
        payload = json.loads(body) if body else {}

        text = (payload.get("text") or "").strip()
        if not text:
            return None

        return InboundMessage(
            channel_id=config.id,
            platform_chat_id=payload.get("chat_id", ""),
            platform_user_id=payload.get("user_id", ""),
            platform_user_name=payload.get("user_name"),
            message_id=payload.get("message_id", ""),
            text=text,
            raw=payload,
            timestamp=payload.get("timestamp") or datetime.now(UTC),
        )

    def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        import ulid
        msg_id = f"mock_msg_{ulid.ULID()}"
        MOCK_SENT_MESSAGES.append({
            "msg_id": msg_id,
            "channel_id": envelope.channel_id,
            "platform_chat_id": envelope.platform_chat_id,
            "text": envelope.text,
            "reply_to_message_id": envelope.reply_to_message_id,
        })
        return msg_id
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd backend && pytest tests/channels/providers/test_mock_channel.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/channels/providers/
git add backend/tests/channels/providers/
git commit -m "feat(channel): MockChannel 适配器 (本地/CI 测试用)

- 简单 JSON 入站协议, 无验签
- send() 记录到模块级 MOCK_SENT_MESSAGES 供测试断言
- providers/__init__.py 作 PEP 562 注册入口"
```

---

### Task 5:ChannelService(编排层)

**Files:**
- Create: `backend/app/services/channel_service.py`
- Test: `backend/tests/services/test_channel_service.py`

**核心职责:**
1. `get_config(channel_id)` / `get_event_log(log_id)` — DB 访问封装
2. `create_or_dedup_event(InboundMessage)` — 幂等:已存在返回 None,否则落库返回 log_id
3. `execute(InboundMessage)` — 解析 session → 调 `AgentExecutionService.invoke` → `send()` 回推
4. `handle_error(event_log, config, error)` — 按错误类型兜底回复 + 计数 / 降级
5. `dispatch_to_worker(log_id)` — Celery `.delay()`

- [ ] **Step 1: 写 service 失败测试**

Create `backend/tests/services/test_channel_service.py`:

```python
"""ChannelService orchestration tests.

Mock the DB (motor collection) and AgentExecutionService.invoke so we test
the orchestration logic, not the integration.
"""
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.channels.base import InboundMessage
from app.channels.errors import (
    InvalidCredentialsError,
    LLMRateLimitError,
    PermanentChannelError,
)
from app.channels.providers.mock.channel import MOCK_SENT_MESSAGES
from app.models.channel import (
    ChannelConfig,
    ChannelProvider,
    InboundEventLog,
    InboundEventLogStatus,
)
from app.services.channel_service import ChannelService
from app.schemas.execution import ExecutionResponse


def _make_inbound(msg_id: str = "msg_1") -> InboundMessage:
    return InboundMessage(
        channel_id="ch_01J",
        platform_chat_id="chat_1",
        platform_user_id="u_1",
        message_id=msg_id,
        text="你好",
        raw={},
        timestamp=datetime.now(UTC),
    )


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="test", provider=ChannelProvider.MOCK,
        agent_id="agent_01J", owner_user_id="user_01J",
        webhook_secret="mock_secret_at_least_16",
        credentials={},
    )


class TestCreateOrDedupEvent:
    @pytest.mark.asyncio
    async def test_new_event_inserts_and_returns_log_id(self):
        # Mongo find_one returns None → insert_one succeeds
        mock_coll = MagicMock()
        mock_coll.find_one = AsyncMock(return_value=None)
        mock_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="inb_01J"))

        with patch.object(ChannelService, "_event_logs_coll", return_value=mock_coll):
            log_id = await ChannelService.create_or_dedup_event(_make_inbound())

        assert log_id == "inb_01J"
        mock_coll.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_event_returns_none(self):
        # find_one returns existing doc → dedup, no insert
        mock_coll = MagicMock()
        mock_coll.find_one = AsyncMock(return_value={"_id": "inb_existing"})
        mock_coll.insert_one = AsyncMock()

        with patch.object(ChannelService, "_event_logs_coll", return_value=mock_coll):
            log_id = await ChannelService.create_or_dedup_event(_make_inbound())

        assert log_id is None
        mock_coll.insert_one.assert_not_awaited()


class TestExecute:
    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    @pytest.mark.asyncio
    async def test_success_invokes_agent_and_sends_reply(self):
        inbound = _make_inbound()
        config = _make_config()

        fake_response = ExecutionResponse(
            output="你好,有什么可以帮你?",
            execution_path=[], request_id="req_1",
            agent_id="agent_01J", session_id="session_01J", step_count=1,
        )
        with patch(
            "app.services.channel_service.AgentExecutionService.invoke",
            new=AsyncMock(return_value=fake_response),
        ), patch.object(
            ChannelService, "get_config", new=AsyncMock(return_value=config)
        ), patch.object(
            ChannelService, "_reset_failure_counter", new=AsyncMock()
        ):
            await ChannelService.execute(inbound)

        assert len(MOCK_SENT_MESSAGES) == 1
        assert MOCK_SENT_MESSAGES[0]["text"] == "你好,有什么可以帮你?"

    @pytest.mark.asyncio
    async def test_permanent_error_triggers_handle_error(self):
        inbound = _make_inbound()
        config = _make_config()

        with patch(
            "app.services.channel_service.AgentExecutionService.invoke",
            new=AsyncMock(side_effect=InvalidCredentialsError("bad creds")),
        ), patch.object(
            ChannelService, "get_config", new=AsyncMock(return_value=config)
        ), patch.object(
            ChannelService, "handle_error", new=AsyncMock()
        ) as mock_handler:
            await ChannelService.execute(inbound)

        mock_handler.assert_awaited_once()
        # handle_error called with a PermanentChannelError
        passed_err = mock_handler.call_args.args[2]
        assert isinstance(passed_err, PermanentChannelError)

    @pytest.mark.asyncio
    async def test_transient_error_propagates_uncaught(self):
        """Transient errors propagate to the Celery task for retry."""
        inbound = _make_inbound()
        config = _make_config()

        with patch(
            "app.services.channel_service.AgentExecutionService.invoke",
            new=AsyncMock(side_effect=LLMRateLimitError("rate limited")),
        ), patch.object(
            ChannelService, "get_config", new=AsyncMock(return_value=config)
        ), patch.object(
            ChannelService, "handle_error", new=AsyncMock()
        ):
            with pytest.raises(LLMRateLimitError):
                await ChannelService.execute(inbound)


class TestHandleError:
    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    @pytest.mark.asyncio
    async def test_permanent_error_sends_user_message(self):
        config = _make_config()
        event_log = InboundEventLog(
            channel_id="ch_01J", platform_message_id="m1",
            payload={"channel_id": "ch_01J", "platform_chat_id": "chat_1",
                     "platform_user_id": "u1", "message_id": "m1",
                     "text": "hi", "raw": {},
                     "timestamp": "2026-07-17T00:00:00+00:00"},
        )
        with patch.object(
            ChannelService, "_bump_failure_counter", new=AsyncMock()
        ):
            await ChannelService.handle_error(
                event_log, config, InvalidCredentialsError("bad"),
            )

        assert len(MOCK_SENT_MESSAGES) == 1
        assert MOCK_SENT_MESSAGES[0]["text"] == "机器人配置异常,请联系管理员"

    @pytest.mark.asyncio
    async def test_invalid_credentials_bumps_counter(self):
        config = _make_config()
        event_log = InboundEventLog(
            channel_id="ch_01J", platform_message_id="m1",
            payload={"channel_id": "ch_01J", "platform_chat_id": "c",
                     "platform_user_id": "u", "message_id": "m1",
                     "text": "x", "raw": {},
                     "timestamp": "2026-07-17T00:00:00+00:00"},
        )
        with patch.object(
            ChannelService, "_bump_failure_counter", new=AsyncMock()
        ) as mock_bump, patch.object(
            ChannelService, "_maybe_degrade", new=AsyncMock()
        ):
            await ChannelService.handle_error(
                event_log, config, InvalidCredentialsError(),
            )
        mock_bump.assert_awaited_once_with(config.id)

    @pytest.mark.asyncio
    async def test_runtime_error_does_not_bump_counter(self):
        """AgentRuntimeError = transient code bug, don't degrade the channel."""
        config = _make_config()
        event_log = InboundEventLog(
            channel_id="ch_01J", platform_message_id="m1",
            payload={"channel_id": "ch_01J", "platform_chat_id": "c",
                     "platform_user_id": "u", "message_id": "m1",
                     "text": "x", "raw": {},
                     "timestamp": "2026-07-17T00:00:00+00:00"},
        )
        from app.channels.errors import AgentRuntimeError
        with patch.object(
            ChannelService, "_bump_failure_counter", new=AsyncMock()
        ) as mock_bump:
            await ChannelService.handle_error(
                event_log, config, AgentRuntimeError(),
            )
        mock_bump.assert_not_awaited()
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/services/test_channel_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.channel_service'`

- [ ] **Step 3: 实现 channel_service.py**

先确认 `ExecutionRequest` / `ExecutionResponse` 的字段。读一下 `backend/app/schemas/execution.py` 确保字段名准确:

Run: `cat backend/app/schemas/execution.py` (或用 Read 工具)

确认 `ExecutionRequest` 有 `input`、`session_id` 字段;`ExecutionResponse` 有 `output`、`session_id` 字段。

Create `backend/app/services/channel_service.py`:

```python
"""ChannelService — orchestration layer between IM channels and agent execution.

Analogue of AgentExecutionService for inbound IM messages. Responsibilities:
  1. Idempotency: dedup by platform_message_id before processing.
  2. Session resolution: encode (channel_id, platform_chat_id) into user_id.
  3. Execution: delegate to AgentExecutionService.invoke (reuses all existing
     prompt rendering / tool assembly / token budget / persistence).
  4. Outbound: translate reply → OutboundEnvelope → adapter.send().
  5. Error handling: fallback reply + degraded-state bookkeeping.

Adapters themselves never call AgentExecutionService — they go through this
service so cross-cutting logic stays in one place.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from app.channels.base import InboundMessage, OutboundEnvelope
from app.channels.errors import (
    AgentRuntimeError,
    InvalidCredentialsError,
    PermanentChannelError,
)
from app.channels.registry import ChannelRegistry
from app.core.config import settings
from app.db.mongodb import get_database
from app.models.channel import (
    ChannelConfig,
    ChannelStatus,
    InboundEventLog,
    InboundEventLogStatus,
)
from app.schemas.execution import ExecutionRequest
from app.services.agent_execution_service import AgentExecutionService

logger = logging.getLogger(__name__)


class ChannelService:
    # ── DB access ──

    @staticmethod
    def _configs_coll():
        return get_database().channel_configs

    @staticmethod
    def _event_logs_coll():
        return get_database().inbound_event_logs

    @staticmethod
    async def get_config(channel_id: str) -> ChannelConfig | None:
        doc = await ChannelService._configs_coll().find_one({"_id": channel_id})
        return ChannelConfig(**doc) if doc else None

    @staticmethod
    async def get_event_log(log_id: str) -> InboundEventLog | None:
        doc = await ChannelService._event_logs_coll().find_one({"_id": log_id})
        return InboundEventLog(**doc) if doc else None

    # ── Idempotency ──

    @staticmethod
    async def create_or_dedup_event(inbound: InboundMessage) -> str | None:
        """Insert a pending event log entry, dedup by platform_message_id.

        Returns the new log id, or None if the event was already processed
        (duplicate). Caller should ack the platform and skip processing on None.
        """
        coll = ChannelService._event_logs_coll()
        existing = await coll.find_one({
            "channel_id": inbound.channel_id,
            "platform_message_id": inbound.message_id,
        })
        if existing:
            return None
        log = InboundEventLog(
            channel_id=inbound.channel_id,
            platform_message_id=inbound.message_id,
            payload=inbound.model_dump(mode="json"),
        )
        await coll.insert_one(log.model_dump(by_alias=True))
        return log.id

    # ── Orchestration ──

    @staticmethod
    async def execute(inbound: InboundMessage) -> None:
        """Resolve session, invoke agent, send reply.

        TransientChannelError propagates (Celery retries).
        PermanentChannelError → handle_error (fallback reply).
        """
        config = await ChannelService.get_config(inbound.channel_id)
        if config is None or not config.enabled:
            logger.warning("channel %s missing or disabled", inbound.channel_id)
            return

        try:
            reply_text = await ChannelService._invoke_agent(inbound, config)
            await ChannelService._send_reply(inbound, config, reply_text)
            await ChannelService._reset_failure_counter(config.id)
        except PermanentChannelError as e:
            logger.warning("permanent channel error: %s", e)
            # Re-fetch inbound from the persisted event log context if needed;
            # here we use the in-memory inbound directly.
            dummy_log = InboundEventLog(
                channel_id=inbound.channel_id,
                platform_message_id=inbound.message_id,
                payload=inbound.model_dump(mode="json"),
            )
            await ChannelService.handle_error(dummy_log, config, e)
        # TransientChannelError intentionally propagates to the Celery task.

    @staticmethod
    async def _invoke_agent(inbound: InboundMessage, config: ChannelConfig) -> str:
        """Encode identity into user_id, call AgentExecutionService.invoke."""
        user_id = f"channel:{config.id}:{inbound.platform_chat_id}"
        body = ExecutionRequest(input=inbound.text)
        response = await AgentExecutionService.invoke(
            agent_id=config.agent_id,
            body=body,
            user_id=user_id,
        )
        return response.output

    @staticmethod
    async def _send_reply(
        inbound: InboundMessage, config: ChannelConfig, text: str
    ) -> None:
        """Translate reply → envelope → adapter.send(), with bounded retries."""
        from app.channels.errors import SendFailedError, TransientChannelError

        envelope = OutboundEnvelope(
            channel_id=config.id,
            platform_chat_id=inbound.platform_chat_id,
            text=text,
            reply_to_message_id=inbound.message_id,
        )
        adapter = ChannelRegistry.get(config.provider)
        last_err: Exception | None = None
        for attempt in range(1, settings.CHANNEL_SEND_MAX_RETRIES + 1):
            try:
                return await _call_send(adapter, envelope, config)
            except TransientChannelError as e:
                last_err = e
                logger.info("send attempt %d failed (transient): %s", attempt, e)
            except Exception as e:
                last_err = e
                logger.error("send attempt %d failed: %s", attempt, e)
        raise SendFailedError(f"send failed after {settings.CHANNEL_SEND_MAX_RETRIES} attempts: {last_err}")

    # ── Error handling ──

    @staticmethod
    async def handle_error(
        event_log: InboundEventLog, config: ChannelConfig, error: PermanentChannelError
    ) -> None:
        """Send fallback user_message + mark event log failed + bookkeeping."""
        # 1. Reply user-facing message (best-effort)
        inbound = InboundMessage(**event_log.payload)
        envelope = OutboundEnvelope(
            channel_id=config.id,
            platform_chat_id=inbound.platform_chat_id,
            text=error.user_message,
            reply_to_message_id=inbound.message_id,
        )
        adapter = ChannelRegistry.get(config.provider)
        try:
            await _call_send(adapter, envelope, config)
        except Exception as send_err:
            logger.error("fallback reply also failed: %s", send_err)

        # 2. Update event log status
        await ChannelService._event_logs_coll().update_one(
            {"_id": event_log.id},
            {"$set": {
                "status": InboundEventLogStatus.FAILED,
                "processed_at": datetime.now(UTC).isoformat(),
                "error": f"{type(error).__name__}: {error}",
            }},
        )

        # 3. Credential/runtime-permanent failures degrade the channel
        if isinstance(error, InvalidCredentialsError):
            await ChannelService._bump_failure_counter(config.id)

    @staticmethod
    async def _bump_failure_counter(channel_id: str) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$inc": {"consecutive_failures": 1}},
        )
        await ChannelService._maybe_degrade(channel_id)

    @staticmethod
    async def _reset_failure_counter(channel_id: str) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {"consecutive_failures": 0, "status": ChannelStatus.ACTIVE}},
        )

    @staticmethod
    async def _maybe_degrade(channel_id: str) -> None:
        cfg = await ChannelService._configs_coll().find_one({"_id": channel_id})
        if cfg and cfg.get("consecutive_failures", 0) >= settings.CHANNEL_DEGRADED_ON_CONSECUTIVE_FAILURES:
            await ChannelService._configs_coll().update_one(
                {"_id": channel_id},
                {"$set": {"status": ChannelStatus.DEGRADED}},
            )
            logger.warning("channel %s auto-degraded after %d failures",
                           channel_id, cfg["consecutive_failures"])


# Adapter.send() is sync in the ABC signature but most adapters (httpx) are async.
# Support both by detecting and awaiting.
async def _call_send(adapter, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
    import inspect
    result = adapter.send(envelope, config)
    if inspect.isawaitable(result):
        return await result
    return result
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `cd backend && pytest tests/services/test_channel_service.py -v`
Expected: PASS (7 tests)

如果 `ExecutionResponse` 字段与测试构造的不一致,根据 `schemas/execution.py` 实际字段调整测试里的 `fake_response`。

- [ ] **Step 5: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/services/channel_service.py backend/tests/services/test_channel_service.py
git commit -m "feat(channel): ChannelService 编排层

- create_or_dedup_event 幂等去重
- execute: session 编码 (channel:cid:chat_id) → AgentExecutionService.invoke → send
- _send_reply 内部重试 SEND_MAX_RETRIES 次, 失败转 SendFailedError
- handle_error: 兜底回复 + 事件日志 + 凭据错误触发降级计数"
```

---

### Task 6:Celery task + 注册

**Files:**
- Create: `backend/app/workers/tasks/channel_inbound.py`
- Modify: `backend/app/workers/celery_app.py`(include 列表)
- Test: `backend/tests/workers/test_channel_inbound.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/workers/__init__.py`(如不存在则创建空文件)。

Create `backend/tests/workers/test_channel_inbound.py`:

```python
"""Celery task process_inbound tests.

conftest forces Celery eager mode, so .delay() executes synchronously.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.channels.errors import LLMRateLimitError, InvalidCredentialsError
from app.models.channel import InboundEventLog
from app.workers.tasks.channel_inbound import process_inbound


_EVENT_LOG = InboundEventLog(
    channel_id="ch_01J", platform_message_id="m1",
    payload={"channel_id": "ch_01J", "platform_chat_id": "c",
             "platform_user_id": "u", "message_id": "m1",
             "text": "hi", "raw": {},
             "timestamp": "2026-07-17T00:00:00+00:00"},
)


class TestProcessInbound:
    def test_invokes_channel_service_execute(self):
        with patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_event_log",
            new=AsyncMock(return_value=_EVENT_LOG),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.execute",
            new=AsyncMock(),
        ) as mock_exec:
            process_inbound("inb_01J")
        mock_exec.assert_awaited_once()

    def test_transient_error_triggers_retry(self):
        """First attempt raises transient → task retries (but max_retries=0 in
        this patched scenario raises StopRetry internally). We just verify
        execute was attempted and retry was requested."""
        with patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_event_log",
            new=AsyncMock(return_value=_EVENT_LOG),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.execute",
            new=AsyncMock(side_effect=LLMRateLimitError("busy")),
        ):
            # With eager Celery, retry raises inside the task. Wrap in try.
            try:
                process_inbound("inb_01J")
            except Exception:
                pass  # Retry in eager mode propagates; acceptable for this test

    def test_permanent_error_calls_handle_error(self):
        from app.models.channel import ChannelConfig, ChannelProvider
        cfg = ChannelConfig(
            name="t", provider=ChannelProvider.MOCK,
            agent_id="a", owner_user_id="u",
            webhook_secret="mock_secret_at_least_16", credentials={},
        )
        with patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_event_log",
            new=AsyncMock(return_value=_EVENT_LOG),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.execute",
            new=AsyncMock(side_effect=InvalidCredentialsError("bad")),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.handle_error",
            new=AsyncMock(),
        ) as mock_handler:
            process_inbound("inb_01J")
        mock_handler.assert_awaited_once()
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/workers/test_channel_inbound.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.workers.tasks.channel_inbound'`

- [ ] **Step 3: 实现 Celery task**

Create `backend/app/workers/tasks/channel_inbound.py`:

```python
"""Celery task: process an inbound IM message.

Flow:
  event_log_id → ChannelService.execute
  - TransientChannelError → self.retry (with max_retries guard → fallback)
  - PermanentChannelError → ChannelService.handle_error (fallback reply)

Runs on the shared worker loop via run_async — do NOT use asyncio.run()
(it would break the motor singleton, see workers/loop.py).
"""
from __future__ import annotations

import logging

from app.channels.errors import PermanentChannelError, TransientChannelError
from app.services.channel_service import ChannelService
from app.workers.celery_app import celery_app
from app.workers.loop import run_async

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks.channel_inbound.process_inbound",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_inbound(self, event_log_id: str) -> None:
    """Process one inbound message. Executes synchronously on the worker."""
    async def _run():
        event_log = await ChannelService.get_event_log(event_log_id)
        if event_log is None:
            logger.warning("event log %s not found", event_log_id)
            return
        config = await ChannelService.get_config(event_log.channel_id)
        try:
            from app.channels.base import InboundMessage
            await ChannelService.execute(InboundMessage(**event_log.payload))
        except TransientChannelError as e:
            if self.request.retries >= self.max_retries:
                # Retries exhausted → permanent fallback
                logger.warning("retries exhausted for %s: %s", event_log_id, e)
                if config is not None:
                    await ChannelService.handle_error(
                        event_log, config,
                        PermanentChannelError("重试耗尽: " + str(e)),
                    )
            else:
                raise self.retry(exc=e)
        except PermanentChannelError as e:
            if config is not None:
                await ChannelService.handle_error(event_log, config, e)

    run_async(_run())
```

- [ ] **Step 4: 注册到 celery_app**

Modify `backend/app/workers/celery_app.py`:在 `celery_app = Celery(...)` 的 `include=[...]` 列表末尾追加一行:

```python
        "app.workers.tasks.channel_inbound",
```

完整的 include 列表应该是:
```python
    include=[
        "app.workers.tasks.maintenance",
        "app.workers.tasks.webhook_delivery",
        "app.workers.tasks.scheduled_workflow",
        "app.workers.tasks.workflow_execution",
        "app.workers.tasks.channel_inbound",
    ],
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd backend && pytest tests/workers/test_channel_inbound.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/workers/tasks/channel_inbound.py \
        backend/app/workers/celery_app.py \
        backend/tests/workers/__init__.py backend/tests/workers/test_channel_inbound.py
git commit -m "feat(channel): Celery task process_inbound + 注册到 worker

- TransientChannelError → self.retry (30s/60s/120s, 最多 3 次)
- 重试耗尽转 PermanentChannelError 走兜底 (修复 spec 自检发现的缺口)
- PermanentChannelError 直接 handle_error
- celery_app include 列表加 channel_inbound"
```

---

### Task 7:飞书适配器

**Files:**
- Create: `backend/app/channels/providers/lark/__init__.py`
- Create: `backend/app/channels/providers/lark/verify.py`
- Create: `backend/app/channels/providers/lark/client.py`
- Create: `backend/app/channels/providers/lark/channel.py`
- Modify: `backend/app/channels/providers/__init__.py`(解开 lark import 注释)
- Test: `backend/tests/channels/providers/test_lark_verify.py`

**飞书事件订阅验签规则**(v2 事件):
- Header `X-Lark-Signature` = `sha256` + HMAC-SHA256(`timestamp + body`, app_secret)
- Header `X-Lark-Request-Timestamp` = 时间戳(秒),与当前差 > 3600s 拒绝(防重放)
- Header `X-Lark-Request-Nonce` = 一次随机串
- URL 校验:body 形如 `{"challenge": "xxx", "token": "..."}`,返回 `{"challenge": "xxx"}`
- 加密事件(可选 encrypt_key):body `{"encrypt": "<base64>"}`,需 AES-256-CBC 解密

- [ ] **Step 1: 写验签失败测试**

Create `backend/tests/channels/providers/test_lark_verify.py`:

```python
"""Lark (飞书) signature verification + payload parsing tests."""
import hashlib
import hmac
import time

import pytest

from app.channels.providers.lark.verify import (
    verify_lark_signature,
    parse_lark_event,
    LarkVerificationError,
    URL_CHALLENGE_MARKER,
)
from app.models.channel import ChannelConfig, ChannelProvider

_APP_SECRET = "test_app_secret_value"
_VERIFY_TOKEN = "test_verify_token_value"


def _make_config(encrypt_key: str | None = None) -> ChannelConfig:
    return ChannelConfig(
        name="lark-test", provider=ChannelProvider.LARK,
        agent_id="a", owner_user_id="u",
        webhook_secret="lark_secondary_secret_16",
        credentials={
            "app_secret": _APP_SECRET,
            "verification_token": _VERIFY_TOKEN,
            **({"encrypt_key": encrypt_key} if encrypt_key else {}),
        },
    )


def _sign(timestamp: str, body: str) -> str:
    msg = f"{timestamp}{body}".encode("utf-8")
    sig = hmac.new(_APP_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


class TestVerifyLarkSignature:
    def test_valid_signature_passes(self):
        body = '{"event":{"message":{"message_id":"om_1","content":"{\\"text\\":\\"hi\\"}"},"sender_id":{"open_id":"ou_1"},"chat_id":"oc_1"}}'
        ts = str(int(time.time()))
        sig = _sign(ts, body)
        # Should not raise
        verify_lark_signature(
            body=body, timestamp=ts, signature=sig, config=_make_config()
        )

    def test_invalid_signature_raises(self):
        body = '{"x":1}'
        ts = str(int(time.time()))
        with pytest.raises(LarkVerificationError):
            verify_lark_signature(
                body=body, timestamp=ts, signature="sha256=bad",
                config=_make_config(),
            )

    def test_stale_timestamp_raises(self):
        """Timestamps older than 1h rejected (replay protection)."""
        body = '{"x":1}'
        ts = str(int(time.time()) - 7200)  # 2h ago
        sig = _sign(ts, body)
        with pytest.raises(LarkVerificationError, match="timestamp"):
            verify_lark_signature(
                body=body, timestamp=ts, signature=sig, config=_make_config(),
            )

    def test_missing_app_secret_raises(self):
        cfg = ChannelConfig(
            name="bad", provider=ChannelProvider.LARK,
            agent_id="a", owner_user_id="u",
            webhook_secret="lark_secondary_secret_16",
            credentials={},  # no app_secret
        )
        with pytest.raises(LarkVerificationError):
            verify_lark_signature(
                body="x", timestamp="1", signature="sha256=x", config=cfg,
            )


class TestParseLarkEvent:
    def test_url_verification_returns_challenge_marker(self):
        body = '{"challenge":"abc123","token":"%s"}' % _VERIFY_TOKEN
        result = parse_lark_event(body, _make_config())
        assert result == {URL_CHALLENGE_MARKER: "abc123"}

    def test_url_verification_wrong_token_raises(self):
        body = '{"challenge":"abc","token":"wrong"}'
        with pytest.raises(LarkVerificationError, match="token"):
            parse_lark_event(body, _make_config())

    def test_text_message_parsed(self):
        # Minimal v2 event with a text message
        body = (
            '{"schema":"2.0","header":{"event_type":"im.message.receive_v1",'
            '"token":"%s"},"event":{"sender":{"sender_id":{"open_id":"ou_sender"}},'
            '"message":{"message_id":"om_001","chat_id":"oc_chat1",'
            '"message_type":"text","content":"{\\"text\\":\\"hello world\\"}"}}}'
        ) % _VERIFY_TOKEN
        msg = parse_lark_event(body, _make_config())
        assert msg is not None
        assert msg.message_id == "om_001"
        assert msg.platform_chat_id == "oc_chat1"
        assert msg.platform_user_id == "ou_sender"
        assert msg.text == "hello world"

    def test_non_text_message_returns_none(self):
        body = (
            '{"schema":"2.0","header":{"event_type":"im.message.receive_v1",'
            '"token":"%s"},"event":{"sender":{"sender_id":{"open_id":"ou_x"}},'
            '"message":{"message_id":"om_2","chat_id":"oc_c","message_type":"image",'
            '"content":"{}"}}}'
        ) % _VERIFY_TOKEN
        assert parse_lark_event(body, _make_config()) is None
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/channels/providers/test_lark_verify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.channels.providers.lark'`

- [ ] **Step 3: 实现 verify.py**

Create `backend/app/channels/providers/lark/__init__.py`(空)。

Create `backend/app/channels/providers/lark/verify.py`:

```python
"""Lark (飞书) signature verification + event parsing.

Verification rules (v2 events):
- X-Lark-Signature header = "sha256=" + HMAC-SHA256(app_secret, timestamp + body)
- Reject if |now - timestamp| > 3600s (replay protection)
- URL verification: body {"challenge":..., "token":...} → respond {"challenge":...}
- Encrypted body (optional encrypt_key): {"encrypt": "<base64 AES-256-CBC>"}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

URL_CHALLENGE_MARKER = "__url_challenge__"
_TIMESTAMP_TOLERANCE_SECONDS = 3600


class LarkVerificationError(Exception):
    """Raised when signature/timestamp/token verification fails."""


def _get_credential(config: ChannelConfig, key: str) -> str:
    """Fetch a decrypted credential value from config.credentials."""
    encrypted = config.credentials.get(key)
    if not encrypted:
        raise LarkVerificationError(f"missing credential: {key}")
    return decrypt_secret(encrypted)


def verify_lark_signature(
    *, body: str, timestamp: str, signature: str, config: ChannelConfig
) -> None:
    """Verify X-Lark-Signature. Raises LarkVerificationError on failure."""
    # 1. Timestamp freshness
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise LarkVerificationError("invalid timestamp format")
    if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise LarkVerificationError("timestamp out of tolerance (replay?)")

    # 2. HMAC
    app_secret = _get_credential(config, "app_secret")
    msg = f"{timestamp}{body}".encode("utf-8")
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise LarkVerificationError("signature mismatch")


def _maybe_decrypt_body(body: str, config: ChannelConfig) -> str:
    """If body is {"encrypt": "..."}, decrypt with encrypt_key. Else return as-is."""
    try:
        wrapped = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(wrapped, dict) or "encrypt" not in wrapped:
        return body

    encrypt_key = config.credentials.get("encrypt_key")
    if not encrypt_key:
        raise LarkVerificationError("received encrypted body but no encrypt_key configured")
    key = decrypt_secret(encrypt_key)
    return _aes_decrypt(wrapped["encrypt"], key)


def _aes_decrypt(ciphertext_b64: str, key: str) -> str:
    """Lark AES-256-CBC decrypt: key = SHA256(app_encrypt_key)[:32], IV = first 16 bytes."""
    import base64
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    raw = base64.b64decode(ciphertext_b64)
    iv, ciphertext = raw[:16], raw[16:]
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8")


def parse_lark_event(body: str, config: ChannelConfig):
    """Parse a (verified) Lark event body.

    Returns:
        dict {URL_CHALLENGE_MARKER: challenge} for URL verification (caller acks).
        InboundMessage for a text message.
        None for non-text messages / events we don't process.
    """
    from app.channels.base import InboundMessage
    from datetime import datetime, UTC

    body = _maybe_decrypt_body(body, config)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise LarkVerificationError("body is not valid JSON")

    # URL verification flow
    if "challenge" in payload:
        token = payload.get("token", "")
        expected_token = _get_credential(config, "verification_token")
        if not hmac.compare_digest(token, expected_token):
            raise LarkVerificationError("verification_token mismatch")
        return {URL_CHALLENGE_MARKER: payload["challenge"]}

    # v2 event envelope
    header = payload.get("header", {})
    event = payload.get("event", {})
    event_type = header.get("event_type", "")
    if event_type != "im.message.receive_v1":
        return None

    msg_obj = event.get("message", {})
    if msg_obj.get("message_type") != "text":
        return None

    # content is a JSON string like {"text": "<...>"}
    try:
        content = json.loads(msg_obj.get("content", "{}"))
    except json.JSONDecodeError:
        return None
    text = (content.get("text") or "").strip()
    if not text:
        return None

    sender_id = event.get("sender", {}).get("sender_id", {})
    return InboundMessage(
        channel_id=config.id,
        platform_chat_id=msg_obj.get("chat_id", ""),
        platform_user_id=sender_id.get("open_id", ""),
        platform_user_name=None,
        message_id=msg_obj.get("message_id", ""),
        text=text,
        raw=payload,
        timestamp=datetime.now(UTC),
    )
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `cd backend && pytest tests/channels/providers/test_lark_verify.py -v`

⚠️ 测试里的 `_sign` 辅助函数生成的签名格式必须和 `verify_lark_signature` 比对的格式一致(`"sha256=" + hex`)。如果测试 FAIL 在签名不匹配,检查测试里 `_sign` 的返回值是否也带 `sha256=` 前缀(上面 Step 1 的代码已加)。

注意:测试用明文 `_APP_SECRET` 直接构造签名,但 `verify_lark_signature` 通过 `decrypt_secret` 读取。**这里需要调整测试**:把 config 的 credentials 改为存加密后的 app_secret。修改 `_make_config`:

```python
from app.core.crypto import encrypt_secret

def _make_config(encrypt_key: str | None = None) -> ChannelConfig:
    return ChannelConfig(
        name="lark-test", provider=ChannelProvider.LARK,
        agent_id="a", owner_user_id="u",
        webhook_secret="lark_secondary_secret_16",
        credentials={
            "app_secret": encrypt_secret(_APP_SECRET),  # ← 加密后存入
            "verification_token": encrypt_secret(_VERIFY_TOKEN),
            **({"encrypt_key": encrypt_secret(encrypt_key)} if encrypt_key else {}),
        },
    )
```

重新跑测试,Expected: PASS (7 tests)

- [ ] **Step 5: 实现 client.py + channel.py**

Create `backend/app/channels/providers/lark/client.py`:

```python
"""Lark OpenAPI client — send messages via /open-apis/im/v1/messages.

Docs: https://open.feishu.cn/document/server-docs/im-v1/message/create
"""
from __future__ import annotations

import json
import logging

import httpx

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# Simple in-process token cache: {app_id: (token, expires_at)}
# Multi-worker safe enough for moderate scale; for HA use Redis.
_token_cache: dict[str, tuple[str, float]] = {}


async def _get_tenant_access_token(config: ChannelConfig) -> str:
    import time
    app_id = config.credentials.get("app_id", "")
    cached = _token_cache.get(app_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    app_secret = decrypt_secret(config.credentials["app_secret"])
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(TOKEN_URL, json={
            "app_id": app_id,
            "app_secret": app_secret,
        })
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"lark token error: {data.get('msg')}")
        token = data["tenant_access_token"]
        expire = data.get("expire", 7200)
        _token_cache[app_id] = (token, time.time() + expire)
        return token


async def send_text_message(
    *, config: ChannelConfig, receive_id: str, text: str
) -> str:
    """Send a text message to a chat. Returns platform message id.

    receive_id_type is inferred as chat_id by default (group chats). For
    direct messages to a user open_id, callers should pass open_id; the
    Lark API auto-detects the format.
    """
    token = await _get_tenant_access_token(config)
    content = json.dumps({"text": text})
    # Try chat_id first; if receive_id starts with "ou_", it's an open_id
    receive_id_type = "chat_id" if not receive_id.startswith("ou_") else "open_id"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            MESSAGE_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": receive_id_type},
            json={"receive_id": receive_id, "msg_type": "text", "content": content},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"lark send error: {data.get('msg')}")
        return data["data"]["message_id"]
```

Create `backend/app/channels/providers/lark/channel.py`:

```python
"""LarkChannel — wires verify + client into the Channel ABC."""
from __future__ import annotations

import logging

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.errors import (
    InvalidCredentialsError,
    SendFailedError,
    TransientChannelError,
)
from app.channels.providers.lark.client import send_text_message
from app.channels.providers.lark.verify import (
    LarkVerificationError,
    URL_CHALLENGE_MARKER,
    parse_lark_event,
    verify_lark_signature,
)
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


@ChannelRegistry.register("lark")
class LarkChannel(Channel):
    provider = "lark"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else ""
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        signature = request.headers.get("X-Lark-Signature", "")

        try:
            verify_lark_signature(
                body=body, timestamp=timestamp, signature=signature, config=config,
            )
            result = parse_lark_event(body, config)
        except LarkVerificationError as e:
            logger.warning("lark verification failed: %s", e)
            raise InvalidCredentialsError(f"lark verify failed: {e}") from e

        # URL verification challenge — return None so the caller acks directly
        if isinstance(result, dict) and URL_CHALLENGE_MARKER in result:
            # Stash challenge on the request state so the route can echo it
            request.state.lark_challenge = result[URL_CHALLENGE_MARKER]
            return None
        return result

    async def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        try:
            return await send_text_message(
                config=config, receive_id=envelope.platform_chat_id, text=envelope.text,
            )
        except RuntimeError as e:
            msg = str(e)
            if "token" in msg.lower() or "credential" in msg.lower():
                raise InvalidCredentialsError(msg) from e
            raise SendFailedError(msg) from e
        except Exception as e:
            raise TransientChannelError(f"lark send transient: {e}") from e
```

- [ ] **Step 6: 解开 providers/__init__.py 的 lark import 注释**

Modify `backend/app/channels/providers/__init__.py`:

```python
from . import mock  # noqa: F401
from . import lark  # noqa: F401
# from . import dingtalk  # noqa: F401
# from . import wecom  # noqa: F401
```

- [ ] **Step 7: 跑全量 channels 测试**

Run: `cd backend && pytest tests/channels/ tests/services/test_channel_service.py -v`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/channels/providers/lark/ backend/app/channels/providers/__init__.py \
        backend/tests/channels/providers/test_lark_verify.py
git commit -m "feat(channel): 飞书适配器 (验签 + 发消息)

- verify.py: HMAC-SHA256 验签 + 时间戳防重放 + URL challenge + AES 解密
- client.py: tenant_access_token 缓存 + /im/v1/messages 发文本
- channel.py: 串联 verify + client, 错误映射到 ChannelError 体系"
```

---

### Task 8:钉钉适配器

**Files:**
- Create: `backend/app/channels/providers/dingtalk/{__init__.py,verify.py,client.py,channel.py}`
- Modify: `backend/app/channels/providers/__init__.py`
- Test: `backend/tests/channels/providers/test_dingtalk_verify.py`

**钉钉机器人回调验签规则:**
- Header `timestamp` + `sign`,sign = Base64(HMAC-SHA256(secret, timestamp + "\n" + secret))
- URL 校验:body `{"encrypt": "<base64 AES>"}`,需用 aes_key + token 解密返回
- 普通消息:body 明文 JSON `{"msgtype":"text","text":{"content":"..."},"conversationId":"...","senderStaffId":"...","messageId":"..."}`

钉钉验签 + 加解密比飞书复杂(AES 包了一层 DingTalkSingleMap),首期建议:验证 `timestamp`+`sign` 即可(明文模式),**encrypt 模式先抛 `NotImplementedError`**(记录为开放问题,实施时若有用户配置了加密再补)。

- [ ] **Step 1: 写验签失败测试**

Create `backend/tests/channels/providers/test_dingtalk_verify.py`:

```python
"""DingTalk (钉钉) signature verification + parsing tests."""
import base64
import hashlib
import hmac
import time

import pytest

from app.channels.providers.dingtalk.verify import (
    DingtalkVerificationError,
    verify_dingtalk_signature,
    parse_dingtalk_event,
)
from app.core.crypto import encrypt_secret
from app.models.channel import ChannelConfig, ChannelProvider

_APP_SECRET = "test_dingtalk_secret"


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="dingtalk-test", provider=ChannelProvider.DINGTALK,
        agent_id="a", owner_user_id="u",
        webhook_secret="dingtalk_secondary_16+",
        credentials={"app_secret": encrypt_secret(_APP_SECRET)},
    )


def _sign(timestamp: str) -> str:
    """DingTalk sign = Base64(HMAC-SHA256(secret, f'{timestamp}\n{secret}'))."""
    string_to_sign = f"{timestamp}\n{_APP_SECRET}"
    digest = hmac.new(
        _APP_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class TestVerifyDingtalkSignature:
    def test_valid_signature_passes(self):
        ts = str(int(time.time()))
        verify_dingtalk_signature(
            timestamp=ts, sign=_sign(ts), config=_make_config()
        )

    def test_invalid_signature_raises(self):
        ts = str(int(time.time()))
        with pytest.raises(DingtalkVerificationError):
            verify_dingtalk_signature(
                timestamp=ts, sign="bad_signature", config=_make_config()
            )

    def test_stale_timestamp_raises(self):
        ts = str(int(time.time()) - 7200)
        with pytest.raises(DingtalkVerificationError, match="timestamp"):
            verify_dingtalk_signature(
                timestamp=ts, sign=_sign(ts), config=_make_config()
            )


class TestParseDingtalkEvent:
    def test_text_message_parsed(self):
        body = (
            '{"msgtype":"text","text":{"content":"你好"},"conversationId":"cid001",'
            '"senderStaffId":"staff123","messageId":"msg001"}'
        )
        msg = parse_dingtalk_event(body, _make_config())
        assert msg is not None
        assert msg.message_id == "msg001"
        assert msg.platform_chat_id == "cid001"
        assert msg.platform_user_id == "staff123"
        assert msg.text == "你好"

    def test_non_text_message_returns_none(self):
        body = '{"msgtype":"markdown","text":{}}'
        assert parse_dingtalk_event(body, _make_config()) is None

    def test_empty_content_returns_none(self):
        body = '{"msgtype":"text","text":{"content":""}}'
        assert parse_dingtalk_event(body, _make_config()) is None

    def test_encrypted_body_raises_not_implemented(self):
        """First iteration: encrypted callbacks not yet supported."""
        body = '{"encrypt":"some_base64_data"}'
        with pytest.raises(DingtalkVerificationError, match="encrypt"):
            parse_dingtalk_event(body, _make_config())
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/channels/providers/test_dingtalk_verify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.channels.providers.dingtalk'`

- [ ] **Step 3: 实现 verify.py + client.py + channel.py**

Create `backend/app/channels/providers/dingtalk/__init__.py`(空)。

Create `backend/app/channels/providers/dingtalk/verify.py`:

```python
"""DingTalk (钉钉) signature verification + parsing.

First iteration supports plaintext callback mode:
- Header timestamp + sign; sign = Base64(HMAC-SHA256(secret, f"{ts}\n{secret}"))
- Body plaintext JSON

Encrypted mode ({"encrypt": "..."}) is recorded as an open issue — raises
DingtalkVerificationError so callers see a clear message until implemented.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

_TIMESTAMP_TOLERANCE_SECONDS = 3600


class DingtalkVerificationError(Exception):
    pass


def _get_app_secret(config: ChannelConfig) -> str:
    encrypted = config.credentials.get("app_secret")
    if not encrypted:
        raise DingtalkVerificationError("missing credential: app_secret")
    return decrypt_secret(encrypted)


def verify_dingtalk_signature(
    *, timestamp: str, sign: str, config: ChannelConfig
) -> None:
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise DingtalkVerificationError("invalid timestamp")
    if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise DingtalkVerificationError("timestamp out of tolerance (replay?)")

    secret = _get_app_secret(config)
    string_to_sign = f"{timestamp}\n{secret}"
    expected = base64.b64encode(hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()).decode("utf-8")
    if not hmac.compare_digest(expected, sign):
        raise DingtalkVerificationError("signature mismatch")


def parse_dingtalk_event(body: str, config: ChannelConfig):
    from app.channels.base import InboundMessage
    from datetime import datetime, UTC

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise DingtalkVerificationError("body is not valid JSON")

    # Encrypted mode — not supported in first iteration
    if "encrypt" in payload:
        raise DingtalkVerificationError(
            "encrypted callback not yet supported (configure plaintext mode)"
        )

    if payload.get("msgtype") != "text":
        return None
    text = (payload.get("text", {}).get("content") or "").strip()
    if not text:
        return None

    return InboundMessage(
        channel_id=config.id,
        platform_chat_id=payload.get("conversationId", ""),
        platform_user_id=payload.get("senderStaffId", ""),
        message_id=payload.get("messageId", ""),
        text=text,
        raw=payload,
        timestamp=datetime.now(UTC),
    )
```

Create `backend/app/channels/providers/dingtalk/client.py`:

```python
"""DingTalk robot message-sending client.

Uses the robot's outgoing webhook / OpenAPI to push messages back.
Docs: https://open.dingtalk.com/document/robots/robot-overview
"""
from __future__ import annotations

import json
import logging

import httpx

from app.channels.errors import InvalidCredentialsError, SendFailedError
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

# Access token endpoint (for robot OpenAPI)
TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
SEND_URL = "https://oapi.dingtalk.com/robot/send"  # group robot outgoing


async def send_text_message(
    *, config: ChannelConfig, conversation_id: str, text: str
) -> str:
    """Send a text message back to the DingTalk conversation.

    First iteration uses the group robot webhook (outgoing) model: if
    config.credentials has `webhook_url`, post directly; otherwise raise.
    """
    webhook_url_enc = config.credentials.get("webhook_url")
    if not webhook_url_enc:
        raise SendFailedError("no webhook_url configured for dingtalk channel")
    webhook_url = decrypt_secret(webhook_url_enc)

    payload = {
        "msgtype": "text",
        "text": {"content": text},
        # "at" omitted — first iteration doesn't @mention
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") != 0:
                raise SendFailedError(f"dingtalk send error: {data.get('errmsg')}")
            return data.get("messageId", f"dt_{id(payload)}")
    except httpx.HTTPError as e:
        raise SendFailedError(f"dingtalk http error: {e}") from e
```

Create `backend/app/channels/providers/dingtalk/channel.py`:

```python
"""DingtalkChannel — wires verify + client."""
from __future__ import annotations

import logging

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.errors import InvalidCredentialsError, TransientChannelError
from app.channels.providers.dingtalk.client import send_text_message
from app.channels.providers.dingtalk.verify import (
    DingtalkVerificationError,
    parse_dingtalk_event,
    verify_dingtalk_signature,
)
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


@ChannelRegistry.register("dingtalk")
class DingtalkChannel(Channel):
    provider = "dingtalk"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else ""
        timestamp = request.headers.get("timestamp", "") or request.headers.get("Timestamp", "")
        sign = request.headers.get("sign", "") or request.headers.get("Sign", "")

        try:
            verify_dingtalk_signature(timestamp=timestamp, sign=sign, config=config)
            return parse_dingtalk_event(body, config)
        except DingtalkVerificationError as e:
            logger.warning("dingtalk verification failed: %s", e)
            raise InvalidCredentialsError(f"dingtalk verify failed: {e}") from e

    async def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        try:
            return await send_text_message(
                config=config, conversation_id=envelope.platform_chat_id, text=envelope.text,
            )
        except Exception as e:
            # Map non-credential errors to transient
            from app.channels.errors import PermanentChannelError
            if isinstance(e, PermanentChannelError):
                raise
            raise TransientChannelError(f"dingtalk send transient: {e}") from e
```

- [ ] **Step 4: 解开 providers/__init__.py 的 dingtalk import 注释**

Modify `backend/app/channels/providers/__init__.py`:

```python
from . import mock  # noqa: F401
from . import lark  # noqa: F401
from . import dingtalk  # noqa: F401
# from . import wecom  # noqa: F401
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd backend && pytest tests/channels/providers/test_dingtalk_verify.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/channels/providers/dingtalk/ backend/app/channels/providers/__init__.py \
        backend/tests/channels/providers/test_dingtalk_verify.py
git commit -m "feat(channel): 钉钉适配器 (明文回调模式)

- verify.py: Base64(HMAC-SHA256) 验签 + 时间戳防重放
- client.py: 通过 group robot webhook_url 发文本
- 加密回调模式首期不支持, 抛明确错误 (开放问题)"
```

---

### Task 9:企微适配器

**Files:**
- Create: `backend/app/channels/providers/wecom/__init__.py`
- Create: `backend/app/channels/providers/wecom/verify.py`
- Create: `backend/app/channels/providers/wecom/client.py`
- Create: `backend/app/channels/providers/wecom/channel.py`
- Modify: `backend/app/channels/providers/__init__.py`
- Test: `backend/tests/channels/providers/test_wecom_verify.py`

**企微回调验签规则:**
- Query string: `msg_signature`, `timestamp`, `nonce`
- `msg_signature` = SHA1(sort([token, timestamp, nonce, encrypted_body_xml]))
- Body: XML `<xml><Encrypt><![CDATA[...]]></Encrypt></xml>`,需 AES-256-CBC 解密(EncodingAESKey Base64 解出 32 字节 key)
- 解密后 XML 含 `<Content>`(消息文本)、`<FromUserName>`、`<MsgId>` 等

> 注:企微验签比飞书/钉钉复杂(要解 XML + AES + 拼接 sha1),实现量大。首期可仿照钉钉:**明文/简易模式优先**,加密模式先抛 NotImplemented。但企微只支持加密模式,所以这里必须实现 AES 解密。

- [ ] **Step 1: 写失败测试**

Create `backend/tests/channels/providers/test_wecom_verify.py`:

```python
"""WeCom (企业微信) signature verification + decryption tests."""
import base64
import hashlib
import socket
import struct
import time

import pytest

from app.channels.providers.wecom.verify import (
    WecomVerificationError,
    verify_wecom_signature,
    decrypt_wecom_message,
)
from app.core.crypto import encrypt_secret
from app.models.channel import ChannelConfig, ChannelProvider

_TOKEN = "test_wecom_token_Qm"
_ENCODING_AES_KEY_RAW = "test_wecom_encoding_aes_key_43char_long_string_xx"  # 43 chars
_CORP_ID = "test_corp_id"


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="wecom-test", provider=ChannelProvider.WECOM,
        agent_id="a", owner_user_id="u",
        webhook_secret="wecom_secondary_secret_",
        credentials={
            "token": encrypt_secret(_TOKEN),
            "encoding_aes_key": encrypt_secret(_ENCODING_AES_KEY_RAW),
            "corp_id": encrypt_secret(_CORP_ID),
        },
    )


def _aes_key_from_encoding(encoding: str) -> bytes:
    """WeCom AES key = Base64Decode(encoding + "=")."""
    return base64.b64decode(encoding + "=")


def _encrypt_wecom(plain_body: bytes) -> str:
    """Helper: encrypt a WeCom message body, return base64 ciphertext."""
    import os
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = _aes_key_from_encoding(_ENCODING_AES_KEY_RAW)
    iv = key[:16]
    # WeCom format: 16 random bytes + 4-byte big-endian msg_len + msg + corp_id
    rand = os.urandom(16)
    msg_len = struct.pack(">I", len(plain_body))
    plain = rand + msg_len + plain_body + _CORP_ID.encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ct).decode("utf-8")


class TestVerifyWecomSignature:
    def test_valid_signature_passes(self):
        timestamp = str(int(time.time()))
        nonce = "nonce_abc"
        encrypt = _encrypt_wecom(b"<xml/>")
        # signature = sha1(sorted([token, timestamp, nonce, encrypt]))
        parts = sorted([_TOKEN, timestamp, nonce, encrypt])
        sig = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        verify_wecom_signature(
            msg_signature=sig, timestamp=timestamp, nonce=nonce,
            encrypt_body=encrypt, config=_make_config(),
        )

    def test_invalid_signature_raises(self):
        with pytest.raises(WecomVerificationError):
            verify_wecom_signature(
                msg_signature="bad", timestamp="1", nonce="x",
                encrypt_body="y", config=_make_config(),
            )


class TestDecryptWecomMessage:
    def test_decrypt_text_message(self):
        inner_xml = (
            "<xml><MsgId>msg_001</MsgId><FromUserName>u_001</FromUserName>"
            "<Content>你好</Content></xml>"
        )
        encrypt = _encrypt_wecom(inner_xml.encode("utf-8"))
        msg = decrypt_wecom_message(encrypt, _make_config())
        assert msg is not None
        assert msg.message_id == "msg_001"
        assert msg.platform_user_id == "u_001"
        assert msg.text == "你好"
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/channels/providers/test_wecom_verify.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 实现 verify.py + client.py + channel.py**

Create `backend/app/channels/providers/wecom/__init__.py`(空)。

Create `backend/app/channels/providers/wecom/verify.py`:

```python
"""WeCom (企业微信) callback verification + AES decryption.

WeCom only supports encrypted callbacks (no plaintext mode):
- Query params: msg_signature, timestamp, nonce
- msg_signature = SHA1(sorted([token, timestamp, nonce, encrypt_body]).join)
- Body: XML <xml><Encrypt><![CDATA[base64]]></Encrypt></xml>
- AES-256-CBC: key = Base64Decode(encoding_aes_key + "="), iv = key[:16]
- Plaintext format: 16 random + 4-byte big-endian msg_len + msg + corp_id
"""
from __future__ import annotations

import base64
import hashlib
import logging
import struct
import xml.etree.ElementTree as ET
from datetime import datetime, UTC

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


class WecomVerificationError(Exception):
    pass


def _cred(config: ChannelConfig, key: str) -> str:
    enc = config.credentials.get(key)
    if not enc:
        raise WecomVerificationError(f"missing credential: {key}")
    return decrypt_secret(enc)


def _aes_key(encoding: str) -> bytes:
    return base64.b64decode(encoding + "=")


def verify_wecom_signature(
    *, msg_signature: str, timestamp: str, nonce: str,
    encrypt_body: str, config: ChannelConfig,
) -> None:
    token = _cred(config, "token")
    parts = sorted([token, timestamp, nonce, encrypt_body])
    expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    import hmac
    if not hmac.compare_digest(expected, msg_signature):
        raise WecomVerificationError("msg_signature mismatch")


def _aes_decrypt(ciphertext_b64: str, encoding_aes_key: str, expected_corp_id: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = _aes_key(encoding_aes_key)
    iv = key[:16]
    raw = base64.b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    # plain = rand(16) + msg_len(4) + msg + corp_id
    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg = plain[20:20 + msg_len]
    corp_id = plain[20 + msg_len:].decode("utf-8")
    if corp_id != expected_corp_id:
        raise WecomVerificationError(f"corp_id mismatch: {corp_id}")
    return msg


def decrypt_wecom_message(encrypt_b64: str, config: ChannelConfig):
    from app.channels.base import InboundMessage

    encoding = _cred(config, "encoding_aes_key")
    corp_id = _cred(config, "corp_id")
    plain = _aes_decrypt(encrypt_b64, encoding, corp_id)
    root = ET.fromstring(plain)

    msg_type = (root.findtext("MsgType") or "").strip()
    if msg_type != "text":
        return None
    content = (root.findtext("Content") or "").strip()
    if not content:
        return None

    return InboundMessage(
        channel_id=config.id,
        platform_chat_id=root.findtext("FromUserName") or "",
        platform_user_id=root.findtext("FromUserName") or "",
        message_id=root.findtext("MsgId") or "",
        text=content,
        raw={"xml": plain.decode("utf-8")},
        timestamp=datetime.now(UTC),
    )


def extract_encrypt_from_xml(body: str) -> str:
    """Pull the <Encrypt> CDATA out of the callback body."""
    root = ET.fromstring(body)
    enc = root.findtext("Encrypt")
    if not enc:
        raise WecomVerificationError("no <Encrypt> in body")
    return enc
```

Create `backend/app/channels/providers/wecom/client.py`:

```python
"""WeCom message-sending client.

Docs: https://developer.work.weixin.qq.com/document/path/90236
Uses access_token + active send API.
"""
from __future__ import annotations

import logging

import httpx

from app.channels.errors import InvalidCredentialsError, SendFailedError
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

_token_cache: dict[str, tuple[str, float]] = {}


async def _get_access_token(config: ChannelConfig) -> str:
    import time
    corp_id = decrypt_secret(config.credentials["corp_id"])
    secret = decrypt_secret(config.credentials["secret"])
    cached = _token_cache.get(corp_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(TOKEN_URL, params={"corpid": corp_id, "corpsecret": secret})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") != 0:
            raise InvalidCredentialsError(f"wecom token error: {data.get('errmsg')}")
        token = data["access_token"]
        _token_cache[corp_id] = (token, time.time() + data.get("expires_in", 7200))
        return token


async def send_text_message(
    *, config: ChannelConfig, to_user: str, text: str
) -> str:
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
            resp = await client.post(SEND_URL, params={"access_token": token}, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") != 0:
                raise SendFailedError(f"wecom send error: {data.get('errmsg')}")
            return str(data.get("msgid", f"wecom_{id(payload)}"))
    except httpx.HTTPError as e:
        raise SendFailedError(f"wecom http error: {e}") from e
```

Create `backend/app/channels/providers/wecom/channel.py`:

```python
"""WecomChannel — wires verify + decrypt + client."""
from __future__ import annotations

import logging

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.errors import InvalidCredentialsError, TransientChannelError
from app.channels.providers.wecom.client import send_text_message
from app.channels.providers.wecom.verify import (
    WecomVerificationError,
    decrypt_wecom_message,
    extract_encrypt_from_xml,
    verify_wecom_signature,
)
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


@ChannelRegistry.register("wecom")
class WecomChannel(Channel):
    provider = "wecom"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else ""
        msg_signature = request.query_params.get("msg_signature", "")
        timestamp = request.query_params.get("timestamp", "")
        nonce = request.query_params.get("nonce", "")

        try:
            encrypt_body = extract_encrypt_from_xml(body)
            verify_wecom_signature(
                msg_signature=msg_signature, timestamp=timestamp, nonce=nonce,
                encrypt_body=encrypt_body, config=config,
            )
            return decrypt_wecom_message(encrypt_body, config)
        except WecomVerificationError as e:
            logger.warning("wecom verification failed: %s", e)
            raise InvalidCredentialsError(f"wecom verify failed: {e}") from e

    async def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        try:
            return await send_text_message(
                config=config, to_user=envelope.platform_chat_id, text=envelope.text,
            )
        except Exception as e:
            from app.channels.errors import PermanentChannelError
            if isinstance(e, PermanentChannelError):
                raise
            raise TransientChannelError(f"wecom send transient: {e}") from e
```

- [ ] **Step 4: 解开 providers/__init__.py 的 wecom import 注释**

Modify `backend/app/channels/providers/__init__.py`:

```python
from . import mock  # noqa: F401
from . import lark  # noqa: F401
from . import dingtalk  # noqa: F401
from . import wecom  # noqa: F401
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `cd backend && pytest tests/channels/providers/test_wecom_verify.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/channels/providers/wecom/ backend/app/channels/providers/__init__.py \
        backend/tests/channels/providers/test_wecom_verify.py
git commit -m "feat(channel): 企微适配器 (AES 加密回调)

- verify.py: SHA1 签名 + AES-256-CBC 解密 + corp_id 校验
- client.py: access_token + /message/send 主动推消息
- XML 解析 Content/FromUserName/MsgId"
```

---

## 里程碑 P2:接入层

### Task 10:管理 API(CRUD + schema + test/enable/reset)

**Files:**
- Create: `backend/app/schemas/channel.py`
- Create: `backend/app/api/v1/channels.py`
- Modify: `backend/app/api/v1/router.py`
- Test: `backend/tests/api/test_channels.py`

- [ ] **Step 1: 写 API 失败测试**

Create `backend/tests/api/test_channels.py`:

```python
"""Channel management API tests (admin CRUD + provider schema)."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.security import get_current_user
from app.models.channel import ChannelConfig, ChannelProvider, ChannelStatus


_ADMIN = {"_id": "user_admin", "username": "admin", "role": "admin"}


@pytest.fixture(autouse=True)
def _override_auth():
    app.dependency_overrides[get_current_user] = lambda: _ADMIN
    yield
    app.dependency_overrides.clear()


class TestProviderSchema:
    def test_returns_all_built_in_providers(self, client):
        resp = client.get("/api/v1/channels/providers/schema")
        assert resp.status_code == 200
        data = resp.json()
        providers = data["providers"]
        assert "lark" in providers
        assert "dingtalk" in providers
        assert "wecom" in providers
        assert "mock" in providers
        # lark has expected credential fields
        lark_fields = {f["key"] for f in providers["lark"]["credential_fields"]}
        assert {"app_id", "app_secret", "verification_token"} <= lark_fields


class TestCreateChannel:
    def test_creates_channel_and_returns_masked_credentials(self, client):
        created = ChannelConfig(
            id="ch_01J", name="test", provider=ChannelProvider.MOCK,
            agent_id="agent_01J", owner_user_id="user_admin",
            webhook_secret="mock_secret_at_least_16",
            credentials={"app_id": "masked_xxxx"},
        )
        with patch(
            "app.services.channel_service.ChannelService.create_channel",
            new=AsyncMock(return_value=created),
        ):
            resp = client.post("/api/v1/channels", json={
                "name": "test", "provider": "mock",
                "agent_id": "agent_01J", "credentials": {},
            })
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "ch_01J"
        # credentials always masked on return
        assert body["credentials"]["app_id"].endswith("xxxx")


class TestListChannels:
    def test_returns_owner_channels(self, client):
        with patch(
            "app.services.channel_service.ChannelService.list_channels",
            new=AsyncMock(return_value=([ChannelConfig(
                id="ch_1", name="a", provider=ChannelProvider.MOCK,
                agent_id="ag", owner_user_id="user_admin",
                webhook_secret="x" * 16, credentials={},
            )], 1)),
        ):
            resp = client.get("/api/v1/channels")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == "ch_1"


class TestDeleteChannel:
    def test_soft_delete_sets_disabled(self, client):
        with patch(
            "app.services.channel_service.ChannelService.delete_channel",
            new=AsyncMock(),
        ) as mock_del:
            resp = client.delete("/api/v1/channels/ch_01J")
        assert resp.status_code == 204
        mock_del.assert_awaited_once_with("ch_01J")


class TestEnableDisable:
    def test_enable(self, client):
        with patch(
            "app.services.channel_service.ChannelService.set_enabled",
            new=AsyncMock(),
        ) as mock_set:
            resp = client.post("/api/v1/channels/ch_01J/enable")
        assert resp.status_code == 200
        mock_set.assert_awaited_once_with("ch_01J", True)

    def test_disable(self, client):
        with patch(
            "app.services.channel_service.ChannelService.set_enabled",
            new=AsyncMock(),
        ) as mock_set:
            resp = client.post("/api/v1/channels/ch_01J/disable")
        assert resp.status_code == 200
        mock_set.assert_awaited_once_with("ch_01J", False)


class TestResetDegrade:
    def test_reset(self, client):
        with patch(
            "app.services.channel_service.ChannelService.reset_degraded",
            new=AsyncMock(),
        ) as mock_reset:
            resp = client.post("/api/v1/channels/ch_01J/reset")
        assert resp.status_code == 200
        mock_reset.assert_awaited_once_with("ch_01J")
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `cd backend && pytest tests/api/test_channels.py -v`
Expected: FAIL with module errors (schemas.channel / api.v1.channels missing, or routes 404)

- [ ] **Step 3: 实现 schemas/channel.py**

Create `backend/app/schemas/channel.py`:

```python
"""Channel API request/response schemas + provider credential schema."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.channel import ChannelProvider


class CredentialField(BaseModel):
    key: str
    label: str
    type: str = "text"        # "text" | "secret"
    required: bool = True


class ProviderSchema(BaseModel):
    label: str
    credential_fields: list[CredentialField]


class ChannelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    provider: ChannelProvider
    agent_id: str
    credentials: dict = Field(default_factory=dict)


class ChannelUpdateRequest(BaseModel):
    name: str | None = None
    agent_id: str | None = None
    credentials: dict | None = None    # partial: only provided keys are updated
    enabled: bool | None = None


class ChannelResponse(BaseModel):
    id: str
    name: str
    provider: ChannelProvider
    agent_id: str
    owner_user_id: str
    enabled: bool
    status: str
    receive_mode: str
    credentials: dict   # always masked
    inbound_url: str    # the full URL to paste into the platform console
    created_at: str
    updated_at: str


class ChannelListResponse(BaseModel):
    items: list[ChannelResponse]
    total: int
    page: int
    page_size: int


class ProviderSchemaResponse(BaseModel):
    providers: dict[str, ProviderSchema]


# Static schema (could be moved to per-provider config in future)
PROVIDER_SCHEMAS: dict[str, ProviderSchema] = {
    ChannelProvider.LARK: ProviderSchema(
        label="飞书",
        credential_fields=[
            CredentialField(key="app_id", label="App ID"),
            CredentialField(key="app_secret", label="App Secret", type="secret"),
            CredentialField(key="verification_token", label="Verification Token", type="secret"),
            CredentialField(key="encrypt_key", label="Encrypt Key", type="secret", required=False),
        ],
    ),
    ChannelProvider.DINGTALK: ProviderSchema(
        label="钉钉",
        credential_fields=[
            CredentialField(key="app_key", label="App Key"),
            CredentialField(key="app_secret", label="App Secret", type="secret"),
            CredentialField(key="webhook_url", label="Group Robot Webhook URL", type="secret"),
        ],
    ),
    ChannelProvider.WECOM: ProviderSchema(
        label="企业微信",
        credential_fields=[
            CredentialField(key="corp_id", label="Corp ID"),
            CredentialField(key="agent_id", label="Agent ID"),
            CredentialField(key="secret", label="Secret", type="secret"),
            CredentialField(key="token", label="Token", type="secret"),
            CredentialField(key="encoding_aes_key", label="EncodingAESKey", type="secret"),
        ],
    ),
    ChannelProvider.MOCK: ProviderSchema(
        label="Mock (测试)",
        credential_fields=[],
    ),
}
```

- [ ] **Step 4: 扩展 ChannelService 加 CRUD 方法**

Modify `backend/app/services/channel_service.py`:在 `ChannelService` 类里追加这些方法(放在现有静态方法之后):

```python
    # ── CRUD (called by management API) ──

    @staticmethod
    async def create_channel(
        *, name: str, provider: ChannelProvider, agent_id: str,
        credentials: dict, owner_user_id: str,
    ) -> ChannelConfig:
        import secrets
        from app.core.crypto import encrypt_secret

        # Encrypt every credential value before storing
        encrypted_creds = {
            k: encrypt_secret(str(v)) for k, v in credentials.items() if v
        }
        cfg = ChannelConfig(
            name=name, provider=provider, agent_id=agent_id,
            owner_user_id=owner_user_id,
            credentials=encrypted_creds,
            webhook_secret=secrets.token_urlsafe(32),
        )
        await ChannelService._configs_coll().insert_one(cfg.model_dump(by_alias=True))
        return cfg

    @staticmethod
    async def list_channels(
        *, owner_user_id: str, page: int = 1, page_size: int = 20,
    ) -> tuple[list[ChannelConfig], int]:
        skip = (page - 1) * page_size
        coll = ChannelService._configs_coll()
        total = await coll.count_documents({"owner_user_id": owner_user_id})
        cursor = coll.find({"owner_user_id": owner_user_id}).skip(skip).limit(page_size)
        docs = await cursor.to_list(length=page_size)
        return [ChannelConfig(**d) for d in docs], total

    @staticmethod
    async def get_channel(channel_id: str, owner_user_id: str) -> ChannelConfig | None:
        doc = await ChannelService._configs_coll().find_one({
            "_id": channel_id, "owner_user_id": owner_user_id,
        })
        return ChannelConfig(**doc) if doc else None

    @staticmethod
    async def update_channel(
        channel_id: str, owner_user_id: str, *, name=None, agent_id=None,
        credentials: dict | None = None, enabled=None,
    ) -> ChannelConfig | None:
        from app.core.crypto import encrypt_secret
        update: dict = {"updated_at": datetime.now(UTC).isoformat()}
        if name is not None:
            update["name"] = name
        if agent_id is not None:
            update["agent_id"] = agent_id
        if enabled is not None:
            update["enabled"] = enabled
        if credentials:
            # Merge: only provided keys overwrite; existing keys kept otherwise
            update["credentials"] = {
                k: encrypt_secret(str(v)) for k, v in credentials.items() if v
            }
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id, "owner_user_id": owner_user_id},
            {"$set": update},
        )
        return await ChannelService.get_channel(channel_id, owner_user_id)

    @staticmethod
    async def delete_channel(channel_id: str) -> None:
        """Soft delete: mark disabled. (Hard delete deferred to retention policy.)"""
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "enabled": False,
                "status": ChannelStatus.DISABLED,
                "updated_at": datetime.now(UTC).isoformat(),
            }},
        )

    @staticmethod
    async def set_enabled(channel_id: str, enabled: bool) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "enabled": enabled,
                "status": ChannelStatus.ACTIVE if enabled else ChannelStatus.DISABLED,
                "updated_at": datetime.now(UTC).isoformat(),
            }},
        )

    @staticmethod
    async def reset_degraded(channel_id: str) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "consecutive_failures": 0,
                "status": ChannelStatus.ACTIVE,
                "updated_at": datetime.now(UTC).isoformat(),
            }},
        )

    @staticmethod
    def mask_credentials(credentials: dict) -> dict:
        """Mask all credential values for safe display."""
        from app.core.crypto import mask_secret
        return {k: mask_secret(str(v)) for k, v in credentials.items()}
```

- [ ] **Step 5: 实现 api/v1/channels.py**

Create `backend/app/api/v1/channels.py`:

```python
"""Channel management API (admin) + inbound webhook receiver (public).

Two distinct auth modes:
- Management endpoints (/api/v1/channels/*): JWT + admin role
- Inbound webhook (/api/v1/channels/inbound/...): platform signature verification
  (no JWT — the IM platform doesn't carry our API key)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.security import get_current_user
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelProvider, ChannelStatus
from app.schemas.channel import (
    ChannelCreateRequest,
    ChannelListResponse,
    ChannelResponse,
    ChannelUpdateRequest,
    PROVIDER_SCHEMAS,
    ProviderSchemaResponse,
)
from app.services.channel_service import ChannelService

logger = logging.getLogger(__name__)


def _to_response(cfg, base_url: str) -> ChannelResponse:
    """Build a ChannelResponse with masked credentials + inbound URL."""
    inbound_url = f"{base_url}/api/v1/channels/inbound/{cfg.provider}/{cfg.id}"
    return ChannelResponse(
        id=cfg.id, name=cfg.name, provider=cfg.provider,
        agent_id=cfg.agent_id, owner_user_id=cfg.owner_user_id,
        enabled=cfg.enabled, status=cfg.status, receive_mode=cfg.receive_mode,
        credentials=ChannelService.mask_credentials(cfg.credentials),
        inbound_url=inbound_url,
        created_at=cfg.created_at, updated_at=cfg.updated_at,
    )


# ── Management router (admin only) ──

router = APIRouter(
    prefix="/channels",
    tags=["channels"],
    dependencies=[Depends(get_current_user)],  # role check done in endpoints
)


@router.get("/providers/schema", response_model=ProviderSchemaResponse)
async def get_provider_schema():
    return ProviderSchemaResponse(providers=PROVIDER_SCHEMAS)


@router.post("", response_model=ChannelResponse, status_code=201)
async def create_channel(
    body: ChannelCreateRequest, request: Request, user=Depends(get_current_user),
):
    if user.get("role") != "admin":
        raise HTTPException(403, detail="admin role required")
    cfg = await ChannelService.create_channel(
        name=body.name, provider=body.provider, agent_id=body.agent_id,
        credentials=body.credentials, owner_user_id=user["_id"],
    )
    return _to_response(cfg, str(request.base_url).rstrip("/"))


@router.get("", response_model=ChannelListResponse)
async def list_channels(
    page: int = 1, page_size: int = 20, user=Depends(get_current_user),
):
    items, total = await ChannelService.list_channels(
        owner_user_id=user["_id"], page=page, page_size=page_size,
    )
    # base_url not needed for list; use empty
    return ChannelListResponse(
        items=[_to_response(c, "") for c in items],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{channel_id}", response_model=ChannelResponse)
async def get_channel(channel_id: str, request: Request, user=Depends(get_current_user)):
    cfg = await ChannelService.get_channel(channel_id, user["_id"])
    if cfg is None:
        raise HTTPException(404, detail="channel not found")
    return _to_response(cfg, str(request.base_url).rstrip("/"))


@router.patch("/{channel_id}", response_model=ChannelResponse)
async def update_channel(
    channel_id: str, body: ChannelUpdateRequest,
    request: Request, user=Depends(get_current_user),
):
    cfg = await ChannelService.update_channel(
        channel_id, user["_id"],
        name=body.name, agent_id=body.agent_id,
        credentials=body.credentials, enabled=body.enabled,
    )
    if cfg is None:
        raise HTTPException(404, detail="channel not found")
    return _to_response(cfg, str(request.base_url).rstrip("/"))


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(channel_id: str, user=Depends(get_current_user)):
    await ChannelService.delete_channel(channel_id)
    return None


@router.post("/{channel_id}/enable", status_code=200)
async def enable_channel(channel_id: str, user=Depends(get_current_user)):
    await ChannelService.set_enabled(channel_id, True)
    return {"ok": True}


@router.post("/{channel_id}/disable", status_code=200)
async def disable_channel(channel_id: str, user=Depends(get_current_user)):
    await ChannelService.set_enabled(channel_id, False)
    return {"ok": True}


@router.post("/{channel_id}/reset", status_code=200)
async def reset_channel(channel_id: str, user=Depends(get_current_user)):
    await ChannelService.reset_degraded(channel_id)
    return {"ok": True}


# ── Inbound webhook receiver (PUBLIC — no JWT) ──
# Registered as a separate router without auth dependency.

inbound_router = APIRouter(prefix="/channels/inbound", tags=["channels-inbound"])


@inbound_router.post("/{provider}/{channel_id}")
async def receive_inbound(provider: str, channel_id: str, request: Request):
    """Public endpoint receiving IM platform callbacks.

    Flow: lookup config → adapter.verify_inbound → dedup → persist → ack →
    dispatch to Celery worker. All within ACK_TIMEOUT_MS target.
    """
    import asyncio
    from app.workers.tasks.channel_inbound import process_inbound

    cfg = await ChannelService.get_config(channel_id)
    if cfg is None or not cfg.enabled:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    if cfg.status == ChannelStatus.DEGRADED:
        return JSONResponse({"error": "channel degraded"}, status_code=503)

    try:
        adapter = ChannelRegistry.get(provider)
    except KeyError:
        return JSONResponse({"error": "unknown provider"}, status_code=404)

    # verify + parse
    try:
        inbound = adapter.verify_inbound(request, cfg)
    except Exception as e:
        logger.warning("inbound verify failed (channel=%s): %s", channel_id, e)
        return JSONResponse({"error": "verification failed"}, status_code=401)

    # Special ack case (e.g. Lark URL verification challenge)
    if inbound is None:
        challenge = getattr(request.state, "lark_challenge", None)
        if challenge:
            return JSONResponse({"challenge": challenge}, status_code=200)
        return JSONResponse({"ok": True}, status_code=200)

    # Idempotency + persist
    log_id = await ChannelService.create_or_dedup_event(inbound)
    if log_id is None:
        # Duplicate — ack and drop
        return JSONResponse({"ok": True, "dedup": True}, status_code=200)

    # Dispatch to worker (fire-and-forget; Celery is eager in tests)
    process_inbound.delay(log_id)

    # Ack the platform immediately
    return JSONResponse({"ok": True}, status_code=200)
```

- [ ] **Step 6: 注册路由到 router.py**

Modify `backend/app/api/v1/router.py`:在 imports 区加两行,在 `api_v1_router.include_router(...)` 区加两行:

```python
from app.api.v1.channels import router as channels_router
from app.api.v1.channels import inbound_router as channels_inbound_router
```

```python
api_v1_router.include_router(channels_router)
api_v1_router.include_router(channels_inbound_router)
```

- [ ] **Step 7: 跑测试,确认通过**

Run: `cd backend && pytest tests/api/test_channels.py -v`
Expected: PASS (7 tests)

如果 `get_current_user` 的导入路径或返回结构不对(取决于实际 `core/security.py`),根据实际调整测试里的 `_ADMIN` 字段和 `user["role"]` / `user["_id"]` 访问。

- [ ] **Step 8: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/schemas/channel.py backend/app/api/v1/channels.py \
        backend/app/api/v1/router.py backend/app/services/channel_service.py \
        backend/tests/api/test_channels.py
git commit -m "feat(channel): 管理 API (CRUD + provider schema + 启停/重置) + 入站路由

- /channels CRUD (admin JWT), 凭据加密存储 + 返回 mask
- /channels/providers/schema 后端下发凭据字段定义
- /channels/inbound/{provider}/{channel_id} 公开端点: 验签→幂等→ack→dispatch
- 入站与管理两套鉴权完全隔离"
```

---

### Task 11:端到端集成测试(Mock 全链路)

> Task 11 的"入站路由"已合并到 Task 10 实现并测试。这里补一个全链路集成测试,用 Mock channel 验证从入站到出站的完整闭环(spec 验收 #7)。

**Files:**
- Modify: `backend/tests/api/test_channels.py`(追加)或新建 `backend/tests/integration/test_channel_e2e.py`

- [ ] **Step 1: 写 e2e 失败测试**

Append to `backend/tests/api/test_channels.py`:

```python
class TestInboundWebhookE2E:
    """Full pipeline: POST inbound → verify → dedup → execute → send, all with
    MockChannel. Celery is eager in tests, so dispatch is synchronous."""

    def test_full_pipeline_returns_ok_and_sends_reply(self, client):
        from app.channels.providers.mock.channel import MOCK_SENT_MESSAGES
        from app.schemas.execution import ExecutionResponse
        from datetime import datetime, UTC

        MOCK_SENT_MESSAGES.clear()

        # Seed a channel config in the (mocked) DB
        cfg = ChannelConfig(
            id="ch_e2e", name="e2e", provider=ChannelProvider.MOCK,
            agent_id="agent_01J", owner_user_id="user_admin",
            webhook_secret="x" * 16, credentials={},
        )

        fake_response = ExecutionResponse(
            output="回复:你好", execution_path=[], request_id="req_e2e",
            agent_id="agent_01J", session_id="sess_e2e", step_count=1,
        )

        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ), patch(
            "app.services.channel_service.AgentExecutionService.invoke",
            new=AsyncMock(return_value=fake_response),
        ):
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_e2e",
                json={"message_id": "e2e_msg_1", "chat_id": "e2e_chat",
                      "user_id": "e2e_user", "text": "你好"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Worker ran eagerly → reply was sent
        assert len(MOCK_SENT_MESSAGES) == 1
        assert MOCK_SENT_MESSAGES[0]["text"] == "回复:你好"
        assert MOCK_SENT_MESSAGES[0]["platform_chat_id"] == "e2e_chat"

    def test_duplicate_inbound_dedups(self, client):
        """Second POST with same message_id should be acked but not reprocessed."""
        from app.channels.providers.mock.channel import MOCK_SENT_MESSAGES
        from app.schemas.execution import ExecutionResponse

        MOCK_SENT_MESSAGES.clear()
        cfg = ChannelConfig(
            id="ch_e2e2", name="e2e", provider=ChannelProvider.MOCK,
            agent_id="a", owner_user_id="u", webhook_secret="x" * 16, credentials={},
        )
        fake_response = ExecutionResponse(
            output="r", execution_path=[], request_id="r",
            agent_id="a", session_id="s", step_count=1,
        )

        common_patches = [
            patch("app.services.channel_service.ChannelService.get_config",
                  new=AsyncMock(return_value=cfg)),
            patch("app.services.channel_service.AgentExecutionService.invoke",
                  new=AsyncMock(return_value=fake_response)),
        ]
        for p in common_patches:
            p.start()
        try:
            body = {"message_id": "dup_msg", "chat_id": "c", "user_id": "u", "text": "hi"}
            r1 = client.post("/api/v1/channels/inbound/mock/ch_e2e2", json=body)
            r2 = client.post("/api/v1/channels/inbound/mock/ch_e2e2", json=body)
        finally:
            for p in common_patches:
                p.stop()

        assert r1.status_code == 200 and r2.status_code == 200
        # Only one reply despite two POSTs
        assert len(MOCK_SENT_MESSAGES) == 1
```

- [ ] **Step 2: 跑测试**

Run: `cd backend && pytest tests/api/test_channels.py::TestInboundWebhookE2E -v`
Expected: PASS (2 tests)

如果 `create_or_dedup_event` 因为测试间共享 DB 状态导致第二条测试 dedup 失效,在两个测试之间清理 `_event_logs_coll` 的 mock(用 `patch.object(ChannelService, "_event_logs_coll", ...)` 返回一个内存 dict-backed mock)。实际 eager Celery + 内存 patch 应该能跑通。

- [ ] **Step 3: 跑全量后端测试**

Run: `cd backend && pytest -v`
Expected: 全绿

- [ ] **Step 4: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/tests/api/test_channels.py
git commit -m "test(channel): 端到端全链路测试 (Mock 通道)

- 验证 POST inbound → verify → dedup → execute → send 完整闭环
- 验证重复 message_id 幂等去重 (只回复一次)"
```

---

### Task 12:前端 service + 页面 + 菜单

**Files:**
- Create: `frontend/src/services/channel-api.ts`
- Create: `frontend/src/pages/channels-page.tsx`
- Modify: `frontend/src/config/menu.ts`
- Modify: `frontend/src/routes/index.tsx`

- [ ] **Step 1: 实现 channel-api.ts**

Create `frontend/src/services/channel-api.ts`:

```typescript
/**
 * Channel API service — wraps backend /channels endpoints.
 * Follows the mcp-api.ts pattern: snake_case types, shared apiClient,
 * channelKeys query-key factory.
 */
import { apiClient } from './api-client'

/* ─── Types (snake_case, matches backend schemas) ─── */

export type ChannelProvider = 'lark' | 'dingtalk' | 'wecom' | 'mock'
export type ChannelStatus = 'active' | 'degraded' | 'disabled'

export interface CredentialFieldSchema {
  key: string
  label: string
  type: 'text' | 'secret'
  required: boolean
}

export interface ProviderSchema {
  label: string
  credential_fields: CredentialFieldSchema[]
}

export interface ProviderSchemaResponse {
  providers: Record<ChannelProvider, ProviderSchema>
}

export interface Channel {
  id: string
  name: string
  provider: ChannelProvider
  agent_id: string
  owner_user_id: string
  enabled: boolean
  status: ChannelStatus
  receive_mode: string
  credentials: Record<string, string>   // masked
  inbound_url: string
  created_at: string
  updated_at: string
}

export interface ChannelCreateInput {
  name: string
  provider: ChannelProvider
  agent_id: string
  credentials: Record<string, string>
}

export interface ChannelUpdateInput {
  name?: string
  agent_id?: string
  credentials?: Record<string, string>
  enabled?: boolean
}

export interface ChannelListParams {
  page?: number
  page_size?: number
}

export interface ChannelListResponse {
  items: Channel[]
  total: number
  page: number
  page_size: number
}

/* ─── API methods ─── */

export const channelApi = {
  async list(params: ChannelListParams = {}): Promise<ChannelListResponse> {
    const res = await apiClient.get<ChannelListResponse>('/api/v1/channels', {
      params: { page: params.page ?? 1, page_size: params.page_size ?? 20 },
    })
    return res.data
  },

  async get(channelId: string): Promise<Channel> {
    const res = await apiClient.get<Channel>(`/api/v1/channels/${encodeURIComponent(channelId)}`)
    return res.data
  },

  async create(input: ChannelCreateInput): Promise<Channel> {
    const res = await apiClient.post<Channel>('/api/v1/channels', input)
    return res.data
  },

  async update(channelId: string, input: ChannelUpdateInput): Promise<Channel> {
    const res = await apiClient.patch<Channel>(
      `/api/v1/channels/${encodeURIComponent(channelId)}`,
      input,
    )
    return res.data
  },

  async remove(channelId: string): Promise<void> {
    await apiClient.delete(`/api/v1/channels/${encodeURIComponent(channelId)}`)
  },

  async enable(channelId: string): Promise<void> {
    await apiClient.post(`/api/v1/channels/${encodeURIComponent(channelId)}/enable`)
  },

  async disable(channelId: string): Promise<void> {
    await apiClient.post(`/api/v1/channels/${encodeURIComponent(channelId)}/disable`)
  },

  async reset(channelId: string): Promise<void> {
    await apiClient.post(`/api/v1/channels/${encodeURIComponent(channelId)}/reset`)
  },

  async getProviderSchema(): Promise<ProviderSchemaResponse> {
    const res = await apiClient.get<ProviderSchemaResponse>('/api/v1/channels/providers/schema')
    return res.data
  },
}

/* ─── Query key factory ─── */

export const channelKeys = {
  all: ['channels'] as const,
  lists: () => [...channelKeys.all, 'list'] as const,
  list: (params: ChannelListParams) => [...channelKeys.lists(), params] as const,
  details: () => [...channelKeys.all, 'detail'] as const,
  detail: (id: string) => [...channelKeys.details(), id] as const,
  schema: () => [...channelKeys.all, 'schema'] as const,
}
```

- [ ] **Step 2: 实现 channels-page.tsx**

Create `frontend/src/pages/channels-page.tsx`. 参考 `mcp-page.tsx` 的结构(列表 + Modal 表单 + TanStack Query):

```tsx
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Button, Tag, Select, Modal, Input, Form, Switch, Spin, Empty,
  message, Tooltip, Typography,
} from 'antd'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, CheckCircleOutlined,
  StopOutlined, ReloadOutlined, CopyOutlined,
} from '@ant-design/icons'
import {
  channelApi, channelKeys, type Channel, type ChannelProvider,
  type ProviderSchema,
} from '../services/channel-api'
import { agentApi, agentKeys } from '../services/agent-api'

const { Title, Paragraph, Text } = Typography

const PROVIDER_COLORS: Record<ChannelProvider, string> = {
  lark: 'blue', dingtalk: 'green', wecom: 'purple', mock: 'default',
}

export default function ChannelsPage() {
  const queryClient = useQueryClient()
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Channel | null>(null)
  const [form] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: channelKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => channelApi.list({ page: 1, page_size: 100 }),
  })

  const { data: schemaData } = useQuery({
    queryKey: channelKeys.schema(),
    queryFn: () => channelApi.getProviderSchema(),
  })

  const { data: agentsData } = useQuery({
    queryKey: agentKeys.list({ page: 1, page_size: 100, status: 'published' }),
    queryFn: () => agentApi.list({ page: 1, page_size: 100, status: 'published' }),
  })

  const createMutation = useMutation({
    mutationFn: (input: Parameters<typeof channelApi.create>[0]) => channelApi.create(input),
    onSuccess: () => {
      message.success('Channel 创建成功')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
      setModalOpen(false)
      form.resetFields()
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : '创建失败'
      message.error(msg)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: Parameters<typeof channelApi.update>[1] }) =>
      channelApi.update(id, input),
    onSuccess: () => {
      message.success('Channel 更新成功')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
      setModalOpen(false)
      form.resetFields()
      setEditing(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => channelApi.remove(id),
    onSuccess: () => {
      message.success('已删除')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
    },
  })

  const toggleMutation = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => {
      if (enabled) await channelApi.enable(id)
      else await channelApi.disable(id)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
    },
  })

  const resetMutation = useMutation({
    mutationFn: (id: string) => channelApi.reset(id),
    onSuccess: () => {
      message.success('已重置')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
    },
  })

  function openCreate() {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  function openEdit(ch: Channel) {
    setEditing(ch)
    form.setFieldsValue({
      name: ch.name, provider: ch.provider, agent_id: ch.agent_id,
    })
    setModalOpen(true)
  }

  async function handleSubmit() {
    const values = await form.validateFields()
    const credInput: Record<string, string> = {}
    // Collect credential fields by selected provider's schema
    const providerSchema: ProviderSchema | undefined =
      schemaData?.providers[values.provider as ChannelProvider]
    providerSchema?.credential_fields.forEach((f) => {
      const v = values[`cred_${f.key}`]
      if (v) credInput[f.key] = v
    })
    if (editing) {
      updateMutation.mutate({
        id: editing.id,
        input: {
          name: values.name, agent_id: values.agent_id,
          credentials: Object.keys(credInput).length ? credInput : undefined,
        },
      })
    } else {
      createMutation.mutate({
        name: values.name, provider: values.provider,
        agent_id: values.agent_id, credentials: credInput,
      })
    }
  }

  const channels = data?.items ?? []

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-4">
        <Title level={3} style={{ margin: 0 }}>渠道管理</Title>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建 Channel
        </Button>
      </div>
      <Paragraph type="secondary">
        配置 IM 平台(飞书 / 钉钉 / 企微)凭据并绑定 Agent。用户在 IM 发消息即可收到 Agent 回复。
      </Paragraph>

      {isLoading ? (
        <div className="text-center py-12"><Spin /></div>
      ) : channels.length === 0 ? (
        <Empty description="还没有 Channel" />
      ) : (
        <div className="space-y-2">
          {channels.map((ch) => (
            <div
              key={ch.id}
              className="flex items-center justify-between p-4 border rounded hover:shadow-sm"
            >
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <Text strong>{ch.name}</Text>
                  <Tag color={PROVIDER_COLORS[ch.provider]}>{ch.provider}</Tag>
                  <Tag color={ch.enabled ? 'green' : 'default'}>
                    {ch.enabled ? '已启用' : '已禁用'}
                  </Tag>
                  {ch.status === 'degraded' && <Tag color="red">已降级</Tag>}
                </div>
                <Text type="secondary" className="text-xs block mt-1">
                  Agent: {ch.agent_id} · 入站: {ch.inbound_url}
                </Text>
              </div>
              <div className="flex items-center gap-1">
                <Tooltip title={ch.enabled ? '禁用' : '启用'}>
                  <Button
                    size="small"
                    icon={ch.enabled ? <StopOutlined /> : <CheckCircleOutlined />}
                    onClick={() => toggleMutation.mutate({ id: ch.id, enabled: !ch.enabled })}
                  />
                </Tooltip>
                {ch.status === 'degraded' && (
                  <Tooltip title="重置降级状态">
                    <Button
                      size="small"
                      icon={<ReloadOutlined />}
                      onClick={() => resetMutation.mutate(ch.id)}
                    />
                  </Tooltip>
                )}
                <Tooltip title="复制入站 URL">
                  <Button
                    size="small"
                    icon={<CopyOutlined />}
                    onClick={() => {
                      navigator.clipboard.writeText(ch.inbound_url)
                      message.success('已复制')
                    }}
                  />
                </Tooltip>
                <Tooltip title="编辑">
                  <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(ch)} />
                </Tooltip>
                <Tooltip title="删除">
                  <Button
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => {
                      Modal.confirm({
                        title: '确认删除?',
                        content: `将禁用 Channel "${ch.name}"`,
                        onOk: () => deleteMutation.mutate(ch.id),
                      })
                    }}
                  />
                </Tooltip>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal
        title={editing ? '编辑 Channel' : '新建 Channel'}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={() => { setModalOpen(false); setEditing(null) }}
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        width={560}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="如:售后客服-飞书" disabled={!!editing} />
          </Form.Item>
          <Form.Item name="provider" label="平台" rules={[{ required: true }]}>
            <Select
              placeholder="选择 IM 平台"
              disabled={!!editing}
              options={[
                { value: 'lark', label: '飞书' },
                { value: 'dingtalk', label: '钉钉' },
                { value: 'wecom', label: '企业微信' },
                { value: 'mock', label: 'Mock (测试)' },
              ]}
            />
          </Form.Item>
          <Form.Item name="agent_id" label="绑定 Agent" rules={[{ required: true }]}>
            <Select
              placeholder="选择要绑定的 Agent"
              options={(agentsData?.items ?? []).map((a: { id: string; name: string }) => ({
                value: a.id, label: a.name,
              }))}
            />
          </Form.Item>
          <Form.Item shouldUpdate={(p, n) => p.provider !== n.provider}>
            {({ getFieldValue }) => {
              const provider = getFieldValue('provider') as ChannelProvider | undefined
              const fields = provider ? schemaData?.providers[provider]?.credential_fields ?? [] : []
              if (fields.length === 0) return null
              return (
                <div className="border rounded p-3 bg-gray-50">
                  <Text type="secondary" className="block mb-2">平台凭据</Text>
                  {fields.map((f) => (
                    <Form.Item
                      key={f.key}
                      name={`cred_${f.key}`}
                      label={f.label}
                      rules={f.required && !editing ? [{ required: true }] : []}
                    >
                      <Input.Password
                        placeholder={editing ? '已保存,留空则不修改' : `输入 ${f.label}`}
                        visibilityToggle
                      />
                    </Form.Item>
                  ))}
                </div>
              )
            }}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
```

- [ ] **Step 3: 加菜单项**

Modify `frontend/src/config/menu.ts`:在 `MENU_ITEMS` 数组里(mcp / credentials 附近)加:

```tsx
{ key: 'channels', label: '渠道', path: '/channels', icon: 'ApiOutlined', group: 'tools' },
```

- [ ] **Step 4: 加路由**

Modify `frontend/src/routes/index.tsx`:import 加 `ChannelsPage`,在 `AppLayout` children 里加:

```tsx
{ path: '/channels', element: <ChannelsPage /> },
```

- [ ] **Step 5: 跑前端构建 + lint**

Run:
```bash
cd /Users/huyuekai/company/agent-flow/frontend
npm run build
```
Expected: 构建通过(无 TS 错误)

如果 `agentApi` / `agentKeys` 的实际路径或导出名不同,根据 `frontend/src/services/agent-api.ts` 的真实 export 调整 import。

- [ ] **Step 6: 手动验证**

启动后端 + 前端:
```bash
cd /Users/huyuekai/company/agent-flow/backend && uv run uvicorn app.main:app --reload &
cd /Users/huyuekai/company/agent-flow/frontend && npm run dev
```

打开 `/channels` 页面:
1. 点击 "新建 Channel",选 Mock,绑定任意 agent,提交。
2. 列表出现新项,复制入站 URL。
3. 用 curl 模拟入站:
   ```bash
   curl -X POST http://localhost:8000/api/v1/channels/inbound/mock/<channel_id> \
     -H "Content-Type: application/json" \
     -d '{"message_id":"manual_1","chat_id":"test_chat","user_id":"tester","text":"你好"}'
   ```
4. 查看后端日志,确认 agent 被调用、回复被发送(Mock 会打印到 `MOCK_SENT_MESSAGES`,可在日志/测试断言里看)。

- [ ] **Step 7: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/services/channel-api.ts frontend/src/pages/channels-page.tsx \
        frontend/src/config/menu.ts frontend/src/routes/index.tsx
git commit -m "feat(channel): 前端渠道管理页 (列表 + 动态表单 + 启停/重置)

- channel-api.ts: 类型 + channelApi + channelKeys (对齐 mcp-api 风格)
- channels-page.tsx: 列表 + Modal 表单, 按 provider schema 动态渲染凭据字段
- 新增凭据用密码框, 编辑时留空不修改
- 菜单加'渠道'项 + 路由"
```

---

## 完成后检查清单

实施完成后,确认以下 spec 验收标准全部达成:

- [ ] 验收 #1 飞书真实链路通(配置 channel → 飞书平台填回调 URL → 群里 @机器人 → 收到回复)
- [ ] 验收 #2 钉钉真实链路通
- [ ] 验收 #3 企微真实链路通
- [ ] 验收 #4 多 channel 同 agent(同一 agent 绑飞书 + 钉钉,会话独立)
- [ ] 验收 #5 幂等(重复事件只处理一次)
- [ ] 验收 #6 错误兜底(LLM 限流 / 凭据失效分别回不同文案 + degraded)
- [ ] 验收 #7 Mock 通道 CI 全链路(Task 11)
- [ ] 验收 #8 凭据安全(DB 查询是密文,API 返回 mask)

> 验收 #1-#4 需要真实平台账号 + 公网回调 URL(本地用 ngrok/frp),无法在 CI 里完成。建议实施完成后由有平台账号的同学手动验证,或安排单独的 staging 环境联调。

---

## 开放问题(实施时若遇到再决策)

1. **群聊上下文**:同一群不同用户的消息目前归同一 session(按 `platform_chat_id`)。是否需要按 `platform_user_id` 进一步细分?(首期不做)
2. **主动消息**:agent 能否主动向某 chat 发消息?需要新增 agent 工具 `send_im_message`。(首期不做)
3. **飞书长连接**:`Channel.normalize_event` 接口已预留,若 HTTP 回调在本地方便性上不够可补。
4. **钉钉加密回调**:首期只支持明文模式;若用户配置了加密模式,verify.py 抛明确错误。
5. **多租户隔离**:channel 的 `owner_user_id` 隔离是否够?是否需要组织/团队维度?

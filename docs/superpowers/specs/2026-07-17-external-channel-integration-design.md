# 外部消息通道接入设计 (External IM Channel Integration)

- **日期**: 2026-07-17
- **状态**: Draft (待评审)
- **作者**: brainstorming session
- **关联文档**: `docs/planning-artifacts/architecture.md`、`docs/planning-artifacts/external-api-design.md`

---

## 1. 背景与目标

### 1.1 现状

Agent Flow 当前对外接入能力:

- **外部 API**(`api/v1/ext/`):API Key 鉴权 + 限流,支持 `invoke` / `stream` / `resume`,访客会话用 `user_id = "{owner}:{visitor_id}"` 编码隔离。
- **出站 Webhook**(`services/webhook_service.py` + Celery `webhook_delivery`):HMAC-SHA256 签名、5 次指数退避、投递日志。
- **访客 Widget**(`agent-flow-widget/`):Preact + Shadow DOM 浮窗,调 `/ext/agents/{id}/invoke/stream`。

**核心缺口**:

1. 没有任何 IM 平台原生接入(飞书 / 钉钉 / 企微零命中)。
2. 没有入站 Webhook 接收端点——现有 webhooks 全是出站投递。
3. 现有接入都是同步请求-响应模型,无法满足 IM 平台"事件回调→几秒内 ack→异步处理→主动调平台 API 回消息"的协议。

### 1.2 目标

构建**通用的多 IM 集成框架**:抽象出统一的 `Channel` 接口,首期内置飞书 / 钉钉 / 企微三大平台适配器,验证抽象层足够通用。

| 维度 | 决策 |
|---|---|
| 产品形态 | 内置适配器 + 后台配置凭据 / 绑定 |
| 回复模式 | 异步处理 + 主动回推 |
| 绑定关系 | 一个 agent 可绑多个 channel |
| 首期范围 | 飞书 + 钉钉 + 企微 三平台同时打通 |
| 接收模式 | 首期统一 HTTP 回调(长连接作为飞书预留扩展点) |
| 错误处理 | 按错误类型区分(临时错误重试 / 永久错误兜底回复) |

### 1.3 非目标 (Out of Scope)

首期不做:

- 富文本 / @提及 / 卡片消息 / 文件 / 图片(只支持纯文本)。
- 群聊多用户上下文(按 chat_id 隔离会话,不区分群内不同用户身份作为独立会话)。
- 流式分块推送("打字机效果")——预留接口,首期只发最终回复。
- 飞书长连接模式——预留 `normalize_event` 接口,后续实现。
- 插件 SDK 机制(外部注册 channel)——首期适配器全部内置。

---

## 2. 整体架构

### 2.1 分层

新增**通道层 (Channel Layer)** 作为 IM 协议与 agent 执行之间的适配边界,位于 `api/` 与 `services/` 之间,不污染 `engine/` 或 `harness/`。通道层只做协议翻译,不感知 LLM / 工具 / graph。

```
┌─────────────────────────────────────────────────────────────┐
│ IM 平台 (飞书/钉钉/企微)                                     │
│  ↑ HTTP 回调事件                 ↓ 平台 OpenAPI 发消息       │
└──────────┬──────────────────────────────────┬───────────────┘
           │ 入站                              │ 出站
┌──────────▼──────────────────────────────────┴───────────────┐
│ 通道层 (Channel Layer)  ← 新增 backend/app/channels/          │
│  ├─ Ingress: api/v1/channels/inbound/{provider}/{channel_id} │
│  ├─ Channel 抽象接口 (InboundMessage / OutboundEnvelope)     │
│  ├─ 适配器: lark / dingtalk / wecom / mock                   │
│  └─ Sender: 调各平台 OpenAPI 发消息                          │
└──────────┬──────────────────────────────────┬───────────────┘
           │ 标准化 InboundMessage             │ 标准化回复
┌──────────▼──────────────────────────────────┴───────────────┐
│ ChannelService (新增 services/) ← 编排层                     │
│  ① 鉴权/验签 (adapter 自管)                                   │
│  ② 会话解析 (channel_id + platform_chat_id → Session)         │
│  ③ 投递 AgentExecutionService.invoke (复用现有执行链)         │
│  ④ 订阅 AppEvent, 转交 Sender 回推                            │
└──────────┬──────────────────────────────────┬───────────────┘
           │ ExecutionRequest                   │ AppEvent
┌──────────▼──────────────────────────────────┴───────────────┐
│ 现有三层 (完全不改)                                          │
│  api/ → engine/harness_integration/ → packages/harness/      │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心设计原则

1. **新增 `backend/app/channels/` 作为独立翻译层**,不污染 `engine/` 或 `harness/`。
2. **入站路由独立 prefix** `api/v1/channels/`,不走 API Key 中间件——IM 平台验签逻辑各家差异极大,必须由 adapter 自管。
3. **首期统一 HTTP 回调**,三平台同一种接收模式。飞书长连接作为预留扩展点(`Channel.normalize_event` 接口),不进首期。
4. **`ChannelService` 复用 `AgentExecutionService.invoke`**,会话落库 / token 限额 / prompt 渲染 / 工具装配 / 审计全部自动生效——IM 通道只是又一种触发源。

---

## 3. Channel 抽象接口

### 3.1 核心数据结构

```python
# backend/app/channels/base.py

class InboundMessage(BaseModel):
    """标准化入站消息。所有 adapter 产出这个,下游不再感知平台。"""
    channel_id: str              # 哪个 channel 配置
    platform_chat_id: str        # 平台会话标识(飞书 chat_id / 钉钉 conversationId / 企微 group_id 或 open_id)
    platform_user_id: str        # 发送者标识(用于 @提及、权限、多租户)
    platform_user_name: str | None = None
    message_id: str              # 平台消息 ID(幂等去重)
    text: str                    # 文本内容(首期只支持纯文本)
    raw: dict                    # 原始 payload(审计/调试/未来富格式)
    timestamp: datetime


class OutboundEnvelope(BaseModel):
    """标准化出站消息。ChannelService 产出,adapter 翻译成平台 API 调用。"""
    channel_id: str
    platform_chat_id: str        # 回到原会话
    text: str
    reply_to_message_id: str | None = None  # 平台消息引用(可选)
```

### 3.2 Channel 接口

每个 IM 平台实现这个接口即可接入:

```python
# backend/app/channels/base.py

class Channel(ABC):
    """IM 平台适配器接口。新增平台 = 实现这个。"""
    provider: ClassVar[str]      # "lark" / "dingtalk" / "wecom" / "mock"

    @abstractmethod
    def verify_inbound(self, request: Request, config: ChannelConfig) -> InboundMessage | None:
        """验签 + 解析 HTTP 回调。
        返回 InboundMessage 表示通过;
        返回 None 表示需要直接 ack(如飞书 URL 校验 challenge)。
        可抛出 AuthError 拒绝。"""

    @abstractmethod
    def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        """调平台 OpenAPI 发消息。返回平台消息 ID(用于落库/更新)。"""

    def normalize_event(self, event: dict, config: ChannelConfig) -> InboundMessage:
        """(可选)长连接模式的事件解析。HTTP 模式可不实现。"""
        raise NotImplementedError
```

### 3.3 接口设计要点

1. **三个核心方法**:`verify_inbound`(验签+解析入站)、`send`(发消息)、`normalize_event`(长连接事件解析,可选)。富文本 / @提及 / 卡片 / 文件 / 群聊均不进首期。
2. **`InboundMessage.text` 首期只支持纯文本**。
3. **`config: ChannelConfig` 作为方法参数传入**,adapter 不持有状态——同一 adapter 类实例可服务多个配置,无状态更安全。

---

## 4. 数据模型

参考现有 `webhook.py` / `mcp_connection.py` 风格(纯 `pydantic.BaseModel` + `generate_id(prefix)` 生成 `{prefix}_{ULID}` 字符串 ID,**无 Document 基类、无 PyObjectId**)。

### 4.1 ChannelConfig

```python
# backend/app/models/channel.py
from enum import StrEnum
from pydantic import BaseModel, Field
from pydantic.config import ConfigDict
from app.models.base import generate_id, utc_now

class ChannelProvider(StrEnum):
    LARK = "lark"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    MOCK = "mock"           # 本地测试 / CI 用

class ChannelStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"

class ChannelConfig(BaseModel):
    """一个 channel = 一组平台凭据 + 绑定关系。"""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("ch"), alias="_id")
    name: str = Field(..., min_length=1, max_length=200)    # "售后客服-飞书"
    provider: ChannelProvider

    # 凭据(加密存储,用 core/crypto.encrypt_secret / decrypt_secret)
    credentials: dict = Field(default_factory=dict)         # {app_id, app_secret, ...} 各平台字段不同

    # 绑定(支持 1 agent : N channel)
    agent_id: str                      # 这个 channel 把消息交给哪个 agent
    owner_user_id: str                 # 谁创建的(权限 / scope)

    # 接收模式
    receive_mode: str = "webhook"      # 预留 "long_poll" 给未来飞书长连接

    # 运行时状态
    enabled: bool = True
    webhook_secret: str                # 二级校验(防伪造回调)
    status: ChannelStatus = ChannelStatus.ACTIVE
    consecutive_failures: int = 0      # 连续失败计数(达阈值自动 degraded)

    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
```

**设计要点:**

- `credentials` 用 `dict` 承载,因为各平台字段不同(飞书 app_id/app_secret/verification_token/encrypt_key,钉钉 app_key/app_secret/robot_code/aes_key/token,企微 corp_id/agent_id/secret/token/encoding_aes_key)。
- 加密用 `core/crypto.py` 的 `encrypt_secret` / `decrypt_secret`(AES-256-GCM,master key 来自 `settings.MODEL_ENCRYPTION_KEY`),DB 被拖库也不泄密。
- `webhook_secret` 由系统生成,附在入站 URL 或 header 里做二级校验,防回调被伪造。
- `status` 三态:`active`(正常)/ `degraded`(连续失败被自动降级,停止接收)/ `disabled`(管理员手动禁用)。

### 4.2 会话映射(复用 Session,零改动)

支持"一个 agent 绑多 channel"的关键。**复用现有 `Session` 模型,不改 schema,只在 `user_id` 编码上扩展**:

| 场景 | `user_id` 编码 |
|---|---|
| 普通登录用户 | `{user_id}` |
| Widget 访客 | `{owner}:{visitor_id}` |
| IM 通道 | `channel:{channel_id}:{platform_chat_id}` |

这样:

- **Session 表零改动**,所有现有 session 查询 / token 限额 / 消息落库逻辑自动适用。
- 同一个 agent 绑 3 个 channel,每个 channel 的每个 chat 都是独立 session,互不干扰。
- 通过 `user_id` 前缀 `channel:` 反查所有 IM 会话,便于后台管理和审计。

### 4.3 InboundEventLog(幂等去重)

```python
# backend/app/models/channel.py (同文件)

class InboundEventLogStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

class InboundEventLog(BaseModel):
    """幂等去重 + 待处理队列。平台会重发,用 message_id 去重。"""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("inb"), alias="_id")
    channel_id: str
    platform_message_id: str          # 唯一键
    payload: dict                     # 完整 InboundMessage(持久化后才能 ack)
    status: InboundEventLogStatus = InboundEventLogStatus.PENDING
    processed_at: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    # TTL: 24h 后自动删除(平台重试窗口一般 < 1h)
```

**双重职责:**

1. **幂等键**:同一 `platform_message_id` 重复入站时直接丢弃。
2. **待处理队列**:InboundMessage 先落库再 ack,worker 从这里取——进程崩溃也不丢消息。

---

## 5. 数据流——一条消息从入站到回推

### 5.1 完整时序

```
飞书用户在群里发 "查一下订单 SO-12345 的状态"
│
▼ ① 平台 POST 事件到我们的 webhook
POST /api/v1/channels/inbound/lark/{channel_id}
│
▼ ② 路由层: 从 path 拿 channel_id → 查 ChannelConfig → 拿密钥
│
▼ ③ adapter 验签 + 解析
LarkChannel.verify_inbound(request, config)
   ├─ 校验签名(防伪造)
   ├─ 如果是 URL 校验 challenge → 直接返回 challenge JSON (返回 None)
   ├─ 解析 event → 填充 InboundMessage
   └─ 返回 InboundMessage
│
▼ ④ 幂等检查 (ChannelService)
   拿 message_id 查 InboundEventLog
   ├─ 已存在 → 直接 ack(重复事件,丢弃)
   └─ 不存在 → 写入 InboundEventLog(status=pending), 继续
│
▼ ⑤ 立即 ack 平台 (关键!避免平台超时重发)
   返回 200 {"code":0}
   ⚠️ 此时 agent 还没开始跑,ack 必须快(目标 < 200ms)
│
▼ ⑥ 异步触发 (丢 Celery, 不阻塞请求)
   ChannelService.dispatch_to_worker(event_log_id)
   → Celery task: channel.process_inbound.delay(event_log_id)
│
─────────────────── 请求线程结束 (已 ack 平台) ───────────────────
│
▼ ⑦ Celery worker 接手
process_inbound(event_log_id)
   → 读 InboundEventLog → ChannelService.execute(InboundMessage)
│
▼ ⑧ 会话解析 + 调 AgentExecutionService
   session = _resolve_session(
       agent_id = channel.agent_id,
       user_id = f"channel:{channel_id}:{platform_chat_id}"
   )
   → AgentExecutionService.invoke(agent_id, session, text)
│
▼ ⑨ 拿到 agent 最终回复 (非流式,首期只要最终文本)
│
▼ ⑩ 回推到 IM 平台
   envelope = OutboundEnvelope(channel_id, platform_chat_id, text=reply)
   → ChannelRegistry.get(provider).send(envelope, config)
   → LarkChannel.send() 调飞书 OpenAPI
│
▼ ⑪ 用户在飞书看到回复
```

### 5.2 关键决策

1. **ack 必须在幂等检查 + 持久化后立即返回,不等 agent**。各平台超时阈值:飞书 3s、钉钉较短、企微 5s。agent 调用动辄 10s+,绝不能在请求线程里等。ack 之前链路(验签 + 解析 + 幂等 + 持久化)目标 < 200ms。
2. **Celery 复用现有 broker**,新增 task 模块注册到 `celery_app.py` 的 `include`,Redis broker / backend 不变,部署零改动。
3. **首期调 `invoke` 而非 `stream`**。IM 场景不需要"打字机效果"(多数 IM 不支持单条消息流式更新)。`invoke` 一次性拿到最终回复,实现简单、出错面小。流式分块推送作为预留扩展点。
4. **两类失败的重试边界明确区分:**

   - **agent 执行失败**(LLM 限流 / 工具失败等):由 `process_inbound` task 的 `TransientChannelError` → Celery `self.retry` 处理(30s/60s/120s,最多 3 次)。重试耗尽转 `PermanentChannelError` 走兜底回复。
   - **发送失败**(`Channel.send()` 调平台 OpenAPI 失败):在 `ChannelService.execute` 内部独立重试,不走 Celery。重试 3 次仍失败则抛 `SendFailedError`(属 `PermanentChannelError`),记录 `InboundDeliveryLog`(可选),并触发 `consecutive_failures` 计数。避免与 agent 执行重试混淆——agent 已经成功产出回复,只是发不出去,不该重跑整个 agent。

   两类失败都计入 `ChannelConfig.consecutive_failures`,达阈值(默认 5)自动 `degraded`。
5. **InboundMessage 必须先持久化再 ack**(第④步写入后才能第⑤步 ack),否则 ack 后 worker 还没拿到,进程崩溃就丢消息。

---

## 6. 实现细节

### 6.1 目录结构

```
backend/app/
├── channels/                          ← 新增,纯翻译层
│   ├── __init__.py
│   ├── base.py                        # Channel ABC, InboundMessage, OutboundEnvelope
│   ├── registry.py                    # ChannelRegistry (注册 / 查找 adapter)
│   ├── errors.py                      # ChannelError 分类
│   └── providers/                     ← 内置适配器
│       ├── __init__.py                # PEP 562: 触发各 adapter 注册
│       ├── lark/
│       │   ├── __init__.py
│       │   ├── channel.py             # LarkChannel(Channel)
│       │   ├── verify.py              # 验签(飞书:HMAC-SHA256 + AES 解密)
│       │   └── client.py              # 调飞书 OpenAPI 发消息
│       ├── dingtalk/
│       │   ├── channel.py             # DingtalkChannel
│       │   ├── verify.py              # 验签(钉钉:timestamp + sign Base64)
│       │   └── client.py
│       ├── wecom/
│       │   ├── channel.py             # WecomChannel
│       │   ├── verify.py              # 验签(企微:msg_signature + EncodingAESKey)
│       │   └── client.py
│       └── mock/
│           └── channel.py             # MockChannel(本地 / CI 用,直接打印)
│
├── api/v1/
│   ├── channels.py                    ← 新增,入站 webhook 路由 + 管理 API
│   └── ...
│
├── services/
│   └── channel_service.py             ← 新增,编排层(类比 AgentExecutionService)
│
├── models/
│   └── channel.py                     ← 新增,ChannelConfig + InboundEventLog
│
├── schemas/
│   └── channel.py                     ← 新增,API 请求 / 响应模型(CRUD)
│
└── workers/tasks/
    └── channel_inbound.py             ← 新增 Celery task
```

**拆分原则:**

- `channels/providers/{provider}/` 每个 adapter 自成一个包,内部拆 `channel.py`(主逻辑) / `verify.py`(验签) / `client.py`(OpenAPI 调用)三文件,避免单文件膨胀。验签逻辑各家差异极大,必须隔离。
- `channel_service.py` 是唯一编排者,所有 adapter 都不直接调 `AgentExecutionService`——走 service 中转,便于加横切逻辑(审计 / 限流 / 错误分类)。

### 6.2 Adapter 注册机制

复用 harness 包 `TOOL_REGISTRY` 已验证的 PEP 562 延迟加载模式,保持代码库风格一致。

```python
# backend/app/channels/registry.py

class ChannelRegistry:
    _registry: dict[str, type[Channel]] = {}

    @classmethod
    def register(cls, provider: str):
        """装饰器: 注册一个 adapter 类。"""
        def wrapper(channel_cls: type[Channel]):
            cls._registry[provider] = channel_cls
            return channel_cls
        return wrapper

    @classmethod
    def get(cls, provider: str) -> Channel:
        """按 provider 名取 adapter 实例(无状态,每次返回新实例)。"""
        if provider not in cls._registry:
            import backend.app.channels.providers  # noqa: F401  # 触发 PEP 562
        return cls._registry[provider]()


# backend/app/channels/providers/__init__.py
from . import lark, dingtalk, wecom, mock  # noqa: F401


# backend/app/channels/providers/lark/channel.py
@ChannelRegistry.register("lark")
class LarkChannel(Channel):
    provider = "lark"
    def verify_inbound(self, request, config): ...
    def send(self, envelope, config): ...
```

**新增平台的成本:** 建 `providers/{name}/` 目录 + 实现三个文件 + 加一行 import。**零改 registry。**

### 6.3 Celery Task

```python
# backend/app/workers/tasks/channel_inbound.py

from app.workers.celery_app import celery_app
from app.workers.loop import run_async

@celery_app.task(
    name="app.workers.tasks.channel_inbound.process_inbound",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_inbound(self, event_log_id: str):
    """处理一条入站消息。worker 进程内执行。"""
    async def _run():
        event_log = await ChannelService.get_event_log(event_log_id)
        channel_config = await ChannelService.get_config(event_log.channel_id)
        try:
            await ChannelService.execute(InboundMessage(**event_log.payload))
        except TransientChannelError as e:
            # Celery 指数退避(30s / 60s / 120s)
            if self.request.retries >= self.max_retries:
                # 重试耗尽 → 转永久错误,走兜底回复
                await ChannelService.handle_error(
                    event_log, channel_config,
                    PermanentChannelError("重试耗尽: " + str(e))
                )
            else:
                raise self.retry(exc=e)
        except PermanentChannelError as e:
            await ChannelService.handle_error(event_log, channel_config, e)

    run_async(_run())  # 复用现有 sync→async 桥(workers/loop.py)
```

注册到 worker:

```python
# backend/app/workers/celery_app.py (改 1 行)
include = [
    "app.workers.tasks.maintenance",
    "app.workers.tasks.webhook_delivery",
    "app.workers.tasks.scheduled_workflow",
    "app.workers.tasks.workflow_execution",
    "app.workers.tasks.channel_inbound",   # ← 新增
]
```

### 6.4 错误分类(C 方案:按错误类型区分)

```python
# backend/app/channels/errors.py

class ChannelError(Exception):
    """基类。"""
    user_message: str = "处理失败,请稍后重试"

class TransientChannelError(ChannelError):
    """临时错误 → Celery 重试。"""
    user_message = "服务繁忙,正在重试..."

class PermanentChannelError(ChannelError):
    """永久错误 → 不重试,兜底回复。"""
    user_message = "处理失败,请联系管理员"

# 具体子类(adapter 抛出)
class LLMRateLimitError(TransientChannelError):
    user_message = "请求过多,请稍后再试"
class ToolExecutionError(TransientChannelError):
    user_message = "工具暂时不可用,正在重试"
class InvalidCredentialsError(PermanentChannelError):
    user_message = "机器人配置异常,请联系管理员"
class AgentRuntimeError(PermanentChannelError):
    user_message = "服务异常,请联系管理员"
```

**`ChannelService.handle_error` 策略:**

- `TransientChannelError` → Celery 正在重试,**暂不回复用户**(避免重试期间反复打扰)。重试耗尽后(`max_retries` 用完)转为 `PermanentChannelError` 路径。
- `PermanentChannelError` → **立即回 `user_message` 给用户** + 写详细日志 + 若是 `InvalidCredentialsError` 则把 channel 标记为 `degraded`,`consecutive_failures` 计数 +1,达阈值(默认 5)自动降级。

### 6.5 配置项

```python
# backend/app/core/config.py 新增
class Settings(BaseSettings):
    # ... 现有配置 ...

    # Channel / IM 集成
    CHANNEL_INBOUND_ACK_TIMEOUT_MS: int = 2000   # ack 前最长耗时,超时也强制 ack
    CHANNEL_EVENT_LOG_TTL_HOURS: int = 24        # InboundEventLog 保留时长
    CHANNEL_MAX_RETRIES: int = 3                 # 临时错误重试次数
    CHANNEL_DEFAULT_REPLY_ON_FAILURE: str = "处理失败,请稍后重试或联系管理员"
    CHANNEL_DEGRADED_ON_CONSECUTIVE_FAILURES: int = 5  # 连续失败次数阈值
```

**注意:** 平台凭据(app_id / secret 等)**不进 config.py**,而是进 `ChannelConfig.credentials`(加密存 DB)。config.py 只放"全局调优参数",都有默认值,开箱即用。

---

## 7. 后台管理 API + 前端配置页

### 7.1 管理 API

挂在 `api/v1/channels/` 下,管理员 JWT 鉴权(复用现有依赖),与现有 `webhooks.py`、`mcp/connections` 风格对齐。

```
# Channel 配置 CRUD
POST   /api/v1/channels                 创建 channel(填凭据 + 选 provider + 绑 agent)
GET    /api/v1/channels                 列出当前用户的 channel
GET    /api/v1/channels/{id}            详情(凭据字段脱敏:只返回 mask 后的 ****)
PATCH  /api/v1/channels/{id}            更新(凭据字段单独走"重新填写"流程)
DELETE /api/v1/channels/{id}            删除(软删除:enabled=False)

# 运维
POST   /api/v1/channels/{id}/test       连通性测试(发一条 "测试消息" 到配置的 chat_id)
POST   /api/v1/channels/{id}/enable     启用 / 禁用
POST   /api/v1/channels/{id}/reset      重置 degraded 状态(凭据修复后手动恢复)

# Provider schema(前端动态表单用)
GET    /api/v1/channels/providers/schema

# 入站 webhook 入口(对外,不走 JWT,走平台验签)
POST   /api/v1/channels/inbound/{provider}/{channel_id}
```

**鉴权与隔离:**

- 管理 API 走现有 JWT 中间件,按 `owner_user_id` 隔离(用户只能看自己的 channel)。
- 凭据写入前用 `core/crypto.py` 加密;读取时**永远不返回明文**,列表 / 详情接口只返回 `{"app_id": "cli_xxx****"}` 这样的 mask 字段。
- 管理 API(JWT/RBAC)与入站 webhook(平台验签)两套鉴权完全隔离。

### 7.2 入站 Webhook 路由设计

路径:`POST /api/v1/channels/inbound/{provider}/{channel_id}`

例如管理员创建一个飞书 channel 得到 `id=abc123`,前端告诉他:

> 请在飞书开放平台 → 事件订阅 → 请求地址 填写:
> `https://your-domain/api/v1/channels/inbound/lark/abc123`

**为什么 path 带 `channel_id`:** 单个 provider 下可能有多个 channel 配置(多个飞书 app),光靠 `provider` 无法定位具体 config,而验签必须先知道用哪个 config 的密钥。path 带 `channel_id` 可 O(1) 定位,无需遍历;`channel_id` 用 ObjectId 不可猜测,配合 `webhook_secret` 二级校验,足够防扫描。

### 7.3 前端配置页

**位置:`frontend/src/pages/channels/`**(新增),参考现有 `webhooks` 页面结构。

```
/channel-management        列表页
  ├─ Channel 列表(表格:名称 / provider 标签 / 绑定 agent / 状态徽标 / 操作)
  ├─ [新建 Channel] 按钮 → 表单抽屉
  └─ 行操作:编辑 / 测试连通性 / 启用禁用 / 删除
```

表单字段按 provider 动态渲染:

| provider | 凭据字段 |
|---|---|
| lark | app_id, app_secret, verification_token, encrypt_key |
| dingtalk | app_key, app_secret, robot_code, aes_key, token |
| wecom | corp_id, agent_id, secret, token, encoding_aes_key |

**前端动态表单的关键设计:** 后端下发 provider schema,前端按 schema 渲染,新增平台前端零改动。

```python
# GET /api/v1/channels/providers/schema
{
  "lark": {
    "label": "飞书",
    "credential_fields": [
      {"key": "app_id", "label": "App ID", "type": "text", "required": True},
      {"key": "app_secret", "label": "App Secret", "type": "secret", "required": True},
      ...
    ]
  },
  ...
}
```

复用现有前端组件:

- 表单抽屉用 AntD `Drawer` + `Form`(参考现有 `agent-form`、`webhook-form`)。
- 凭据字段用密码框 + "重新填写"按钮(已有值时显示 `****`,点击才展开输入)。
- 平台切换用 AntD `Select`,onChange 时重新拉对应 schema。
- agent 绑定下拉风格复用 `<ToolSelector />`。

### 7.4 侧边栏菜单

在现有侧边栏加一项(参考现有 "Webhooks" / "Triggers" 的位置):

```
配置 / Channels   ← 新增
```

路由 `/channel-management`,权限走现有 RBAC(需 `channels:manage` scope,新增到角色模型)。

### 7.5 安全要点

1. **凭据永远不回明文**:列表 / 详情接口对 `credentials` 字段做 mask。
2. **入站 webhook URL 不可猜测**:`channel_id`(ObjectId) + `webhook_secret` 双因素。
3. **凭据加密**:复用 `core/crypto.py`,DB 被拖库也不泄密。
4. **管理 API 与入站 webhook 鉴权完全隔离**(JWT/RBAC vs 平台验签)。

---

## 8. 验收标准

首期完成后,以下场景必须可用:

1. **飞书**:管理员后台创建 channel(填 app_id/secret 等 + 绑 agent),在飞书开放平台填回调 URL,在飞书群里 @机器人发文本,收到 agent 文本回复。
2. **钉钉**:同上,在钉钉群里发文本收到回复。
3. **企微**:同上,在企微应用里发文本收到回复。
4. **多 channel 同 agent**:同一 agent 绑飞书 + 钉钉两个 channel,两边会话独立、互不干扰。
5. **幂等**:平台重发同一事件,只处理一次,用户只收到一条回复。
6. **错误兜底**:LLM 限流时回 "请求过多,请稍后再试";凭据失效时回 "机器人配置异常" 并标记 channel degraded。
7. **Mock 通道**:CI 中用 `MockChannel` 测全链路,不依赖任何外部服务。
8. **凭据安全**:DB 直接查询 `ChannelConfig` 看到的 credentials 是密文;管理 API 返回的是 mask。

---

## 9. 开放问题(留待实施时决策)

1. **群聊上下文**:同一群不同用户的消息目前都归到同一 session(按 `platform_chat_id`)。未来是否要按 `platform_user_id` 进一步细分?首期不做。
2. **主动消息**:agent 能否主动向某个 chat 发消息(非回复)?需要 agent 工具(类似 `send_lark_message`)。首期不做。
3. **多租户**:channel 的 `owner_user_id` 隔离是否够?还是需要更严格的组织 / 团队维度?待实施时验证。
4. **飞书长连接**:接口已预留(`normalize_event`),实施时若 HTTP 回调模式在本地方便性上不够,可补长连接。

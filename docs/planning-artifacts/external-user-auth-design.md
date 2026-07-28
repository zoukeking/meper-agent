# 终端用户身份认证接入规范

> 对应能力：第三方接入方代表的真实终端用户身份认证与会话隔离。
> 日期：2026-07-21
> 状态：方案已确认，待实施
> 关联文档：[`external-api-design.md`](./external-api-design.md)、[`implementation-artifacts/5-3-mcp-connection-management.md`](../implementation-artifacts/5-3-mcp-connection-management.md)

---

## 0. 文档目的与范围

本文档定义 **agent-flow 与第三方接入方之间关于"终端用户身份"的对接契约**，以及 agent-flow 内部实现该契约的最小改动。

**适用读者**：
- 第三方接入方（实现 introspection 端点、调用 `/api/v1/ext/*`）
- agent-flow 后端开发者

**不包含**：API Key 体系本身、MCP 连接管理 CRUD、MCP server 内部鉴权实现。

---

## 1. 背景与核心问题

### 1.1 当前链路的缺口

agent-flow 目前的外部接入链路：

```
第三方接入方 ──[API Key]──> agent-flow ──[静态 auth_config]──> MCP server
                 │
                 └─ widget 传 visitor_id（浏览器 localStorage UUID）
                     └─ 后端拼成 {owner_user_id}:{visitor_id} 当 user_id
```

**缺口**：
1. **API Key 只代表"接入方"，不代表"接入方背后的真实人类"**。
2. **`visitor_id` 是前端生成的 UUID**，不可信、不持久、不跨设备、无任何用户属性。后端把它和 `owner_user_id` 拼成复合 user_id（`backend/app/api/v1/ext/agents.py:150`），仅能做会话隔离，无法支撑用户级权限决策。
3. **MCP server 完全拿不到调用者身份**。所有用户共享 MCP connection 的静态凭证，无法做 per-user 权限隔离。

### 1.2 两个身份维度的拆分

| 维度 | 解决的问题 | 当前实现 | 本规范目标 |
|------|------------|----------|------------|
| **接入方身份 (Principal)** | 谁在调 API？= 哪个第三方服务 | API Key + `owner_user_id` | 不变 |
| **终端用户身份 (End-User)** | 谁在使用？= 真实人类 | 缺失（用 visitor_id 凑合） | 新增 |

两者**正交**：同一个 API Key 下可以有任意多个终端用户。

### 1.3 信任模型

**核心问题**：如何防止接入方传的用户身份被伪造？

将"防伪造"拆成两个本质不同的威胁：

- **威胁 A — 接入方自己作弊（声明了假用户）**：本质是业务/合同问题，不是密码学问题。技术只能做审计、配额、事后追责。接入方与其终端用户的关系发生在 agent-flow 边界之外，agent-flow 不做背书。
- **威胁 B — 外部攻击者冒充（无 Key 的人伪造调用）**：这才是技术能防住的。攻击者要同时具备有效 API Key + 合法 user_token + 通过 introspection。

**设计原则**：agent-flow 不做接入方用户系统的真实性背书；只负责可信地校验接入方声明的 token、把身份信息传到下游。

---

## 2. 架构总览

### 2.1 整体流程

```
┌──────────────┐
│ 终端用户浏览器│  (接入方前端)
└──────┬───────┘
       │ ① 登录接入方系统,拿到 user_token
       ↓
┌──────────────────────────────────────────────────────────┐
│ 接入方前端                                                 │
│   Authorization: Bearer af_live_xxx        (接入方凭证)   │
│   X-User-Token:  Bearer {user_token}       (终端用户凭证) │
└──────┬───────────────────────────────────────────────────┘
       │ ② 调 /api/v1/ext/*
       ↓
┌──────────────────────────────────────────────────────────┐
│ agent-flow                                                │
│  a. 校验 API Key (现有 ApiKeyPrincipal)                   │
│  b. 若 api_key.user_info_url 有值(开启回调验证):           │
│       - X-User-Token 必须有(否则 401 missing_user_token)  │
│       - 回调接入方 introspection 端点(带 Redis 缓存)       │
│       - user_id = f"{owner}:{sub}"                        │
│       - MCP 调用透传 user_token                           │
│  c. 若 api_key.user_info_url 为空(未开启):                 │
│       - 走现有 visitor_id 逻辑,完全不变                    │
│       - MCP 调用用 auth_config 的 static 凭证              │
└──────┬───────────────────────────────────────────────────┘
       │ ③ 调 MCP server
       ↓
┌──────────────────────────────────────────────────────────┐
│ MCP server (自研,与接入方同一身份体系)                     │
│   Authorization: Bearer {user_token 或 static token}      │
│   ↓ 用自己的鉴权体系解析 token,做权限决策                  │
└──────────────────────────────────────────────────────────┘
```

### 2.2 两种模式（由 `api_key.user_info_url` 是否为空决定）

| 模式 | 触发条件 | user_id 构成 | MCP 凭证 |
|------|----------|--------------|----------|
| **回调验证模式** | `user_info_url` 有值 | `f"{owner}:{sub}"` | 透传 `X-User-Token` |
| **兼容模式（现有）** | `user_info_url` 为空 | `f"{owner}:{visitor_id}"` | `auth_config` 的 static |

两种模式互斥，不跨模式降级。配了 url 就强制走回调验证，缺 token 直接 401。

---

## 3. 接入方对接规范（对外契约）

### 3.1 双凭证请求格式

回调验证模式下，所有 `/api/v1/ext/*` 请求必须同时带两个 header：

```http
POST /api/v1/ext/agents/{agent_id}/invoke HTTP/1.1
Host: api.agent-flow.com
Authorization: Bearer af_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   # ① 接入方凭证
X-User-Token: Bearer {接入方用户 token}                        # ② 终端用户凭证
Content-Type: application/json

{
  "message": "帮我查一下订单",
  "session_id": "sess_xxx"
}
```

**两条铁律**：
- `Authorization`：必须是 agent-flow 颁发的 API Key（`af_live_` 前缀）。接入方身份。
- `X-User-Token`：接入方自己颁发的用户 token。终端用户身份。agent-flow 会回调接入方校验，**不在本地存储**。

两个凭证**正交**。**为什么不用同一个 `Authorization`**：两者是不同维度的凭证，复用会导致语义冲突、调试困难。

**兼容模式**下不需要 `X-User-Token`，沿用现有的 `visitor_id`（body/query 参数）。

### 3.2 Introspection 接口规范（RFC 7662 子集）

接入方在创建/编辑 API Key 时，向 agent-flow 提供一个 introspection 端点 URL（填入 `user_info_url` 字段）。agent-flow 调用该端点校验用户 token。

#### 3.2.1 请求格式（agent-flow → 接入方）

```http
POST /oauth/introspect HTTP/1.1
Host: api.partner.com
Content-Type: application/x-www-form-urlencoded

token={接入方用户 token，URL-encoded}
```

**约定**：
- 方法必须为 `POST`
- `Content-Type` 必须为 `application/x-www-form-urlencoded`
- 请求体仅含一个字段 `token`（URL-encoded）
- **不带调用方鉴权**：introspection 本质是"问 token 合不合法"，攻击者要查询必须先拿到 token，而拿到 token 的人本来就能问接入方"我是谁"。接入方端点不校验调用方。

#### 3.2.2 成功响应（HTTP 200）

```json
{
  "active": true,
  "sub": "user-12345",
  "username": "zhangsan",
  "email": "zhangsan@partner.com",
  "exp": 1735689600,
  "attrs": {
    "display_name": "张三",
    "dept": "销售一部"
  }
}
```

**字段约束**：

| 字段 | 必需 | 类型 | 说明 |
|------|------|------|------|
| `active` | ✅ | bool | token 是否有效 |
| `sub` | ✅（active=true 时） | string | 接入方侧稳定用户 ID（**不随 token 变化**），最长 128 字符 |
| `exp` | ✅（active=true 时） | int | token 过期时间，Unix 时间戳（秒） |
| `username` | 推荐 | string | 登录名 |
| `email` | 推荐 | string | 邮箱 |
| `attrs` | 可选 | object | 业务属性 |

**关键约束**：
- `sub` 必须稳定（同一用户每次 introspect 返回相同的 `sub`），否则会话归属会断裂。
- `active: false` 时只需返回 `{"active": false}`，其余字段可省略。

#### 3.2.3 失败响应

**RFC 7662 规定：token 无效时仍返回 HTTP 200 + `{"active": false}`**，不要返回 4xx。

| HTTP | 含义 | agent-flow 处理 |
|------|------|-----------------|
| 200 + `{"active": false}` | token 无效/过期/被撤销 | 返回 401 `invalid_user_token` |
| 200 + `{"active": true, ...}` | 有效 | 继续 |
| 4xx / 5xx / 超时 | 接入方端点异常 | 见 §3.3 降级策略 |

#### 3.2.4 为什么用 RFC 7662

- 接入方有 OAuth2/OIDC server（Auth0 / Keycloak / Authing 等）→ 端点开箱即用。
- 接入方没有 → 实现一个标准 POST 端点即可，有大量开源参考。
- 工具链成熟：标准请求格式，可用 curl/postman 直接调试。

### 3.3 错误码与降级策略

#### 3.3.1 对接入方的错误响应

| 场景 | HTTP | code |
|------|------|------|
| 回调验证模式下缺 `X-User-Token` | 401 | `missing_user_token` |
| introspect 返回 `active: false` | 401 | `invalid_user_token` |
| introspect 接口 4xx/5xx（首次，无缓存） | 503 | `user_service_unavailable` |
| introspect 接口超时（首次，无缓存） | 504 | `user_service_timeout` |

**响应体格式**（与现有 ext 错误格式一致）：

```json
{
  "error": {
    "code": "invalid_user_token",
    "message": "User token is invalid or expired.",
    "request_id": "req_xxx"
  }
}
```

#### 3.3.2 降级策略（introspect 失败但 Redis 有缓存）

**原则**：缓存命中时，即使接入方 introspection 服务异常，也尽量**降级服务**而非直接失败。

```
introspect 调用失败时:
  if Redis 有该 token 的缓存(未过期):
      使用缓存结果继续,响应头加 X-User-Auth-Stale: true
      异步告警(不阻塞请求)
  else:
      按错误码表返回 503/504
```

缓存 TTL 上限为硬编码值（60s），不延长。超过即视为失效。

#### 3.3.3 重试与超时

- introspection 调用超时：硬编码 3 秒
- 不重试（避免雪崩放大接入方压力）
- 接入方 introspection 接口应保证幂等（同一 token 多次查询结果一致）

---

## 4. agent-flow 侧实现规范（对内契约）

### 4.1 数据模型改动（最小化）

#### 4.1.1 ApiKey 新增字段

`backend/app/models/api_key.py`：

```python
class ApiKey(BaseModel):
    # ... 所有现有字段不变 ...

    user_info_url: str = Field(
        default="",
        max_length=500,
        description="接入方 introspection 端点 URL。空=兼容模式(visitor_id);有值=回调验证模式(X-User-Token)",
    )
```

**唯一新增字段**。语义：
- 空 → 兼容模式（现有 visitor_id 逻辑，完全不变）
- 有值 → 回调验证模式（强制 X-User-Token + introspection + token 透传）

#### 4.1.2 McpConnection 无改动

`auth_mode` 字段取消——MCP 凭证来源完全由"当前请求有没有 user_token"运行时推断，不需要在 MCP connection 上配置。

#### 4.1.3 不新增 EndUser 模型

会话归属直接用 `f"{owner_user_id}:{sub}"`——sub 本身就是稳定的（由接入方保证）。不需要 agent-flow 颁发额外的 `end_user_id`，也不需要 EndUser 表。

历史 session（visitor_id 模式生成）和新 session（sub 模式生成）并存，session 表结构不变，不需要迁移。

### 4.2 Introspection 客户端

`backend/app/services/user_auth_service.py`（新增）：

```python
class UserAuthService:
    """调用接入方 introspection 端点 + Redis 缓存。"""

    CACHE_TTL = 60          # 硬编码
    INTROSPECT_TIMEOUT = 3  # 硬编码

    async def introspect(
        self,
        user_info_url: str,
        user_token: str,
    ) -> IntrospectionResult:
        """返回 introspection 结果。带 Redis 缓存。失败降级见 §3.3.2。"""
        cache_key = f"introspect:{sha256(user_token.encode()).hexdigest()}"
        cached = await self._get_cached(cache_key)
        if cached and not cached.expired:
            return cached

        try:
            result = await self._call_introspect(user_info_url, user_token)
        except (TimeoutError, ServiceError):
            if cached:  # 降级用 stale 缓存
                return cached.as_stale()
            raise

        await self._set_cache(cache_key, result, ttl=self.CACHE_TTL)
        return result
```

**缓存 key**：`introspect:{sha256(token)}`，TTL 60 秒。**原始 token 永不缓存**（只缓存 hash + introspection 结果）。

### 4.3 Ext 路由集成

`backend/app/api/v1/ext/__init__.py`，在现有 `auth_and_rate_limit` 旁加组合 Depends：

```python
async def auth_and_resolve_user(
    request: Request,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> tuple[ApiKeyPrincipal, ResolvedUser]:
    """
    组合依赖:校验 API Key + 解析终端用户身份。
    按 api_key.user_info_url 分叉两种模式。
    """
    api_key = await api_key_service.get_by_id(principal.key_id)

    if api_key.user_info_url:
        # 回调验证模式
        user_token = _extract_user_token(request)  # 从 X-User-Token 提取 Bearer 部分
        if not user_token:
            raise ExtAuthError("missing_user_token")
        result = await user_auth_service.introspect(api_key.user_info_url, user_token)
        if not result.active:
            raise ExtAuthError("invalid_user_token")
        user_id = f"{principal.owner_user_id}:{result.sub}"
        return principal, ResolvedUser(user_id=user_id, user_token=user_token)
    else:
        # 兼容模式:沿用现有 visitor_id 逻辑
        visitor_id = _extract_visitor_id(request)
        user_id = f"{principal.owner_user_id}:{visitor_id}"
        return principal, ResolvedUser(user_id=user_id, user_token=None)


@dataclass
class ResolvedUser:
    user_id: str
    user_token: str | None   # 回调验证模式下有值,供 MCP 透传;兼容模式为 None
```

现有 ext 路由改造（`backend/app/api/v1/ext/agents.py`、`workflows.py` 等）：
- 把原来手动拼接 `{owner_user_id}:{visitor_id}` 的几处（`:150`、`:186`、`:233`、`:284`、`:323`、`:368`）改成用 `resolved_user.user_id`
- 把 Depends 从 `auth_and_rate_limit` 换成 `auth_and_resolve_user`

### 4.4 MCP 调用时的凭证传递

#### 4.4.1 核心思路：token 透传，MCP 自治鉴权

MCP server 本身已有完整的鉴权体系（解析 token、查权限、做决策）。agent-flow 调 MCP 时只做一件事：**把正确的 token 放进 `Authorization` header**。

**前提**：MCP server 和接入方共用同一身份体系（同一 SSO、或 MCP 由接入方提供、或 MCP 对接了接入方的 token 校验能力）。在这个前提下，接入方的 user_token 对 MCP server 是原生可识别的。

#### 4.4.2 凭证来源（运行时推断）

```
调 MCP server 前:
  current_user_token = get_user_token_context()   # 从 ContextVar 读
  if current_user_token:                          # 回调验证模式(外部用户)
      mcp_authorization = f"Bearer {current_user_token}"
  else:                                           # 兼容模式或平台用户
      mcp_authorization = mcp_connection.auth_config 的 static 凭证
```

agent-flow 调 MCP server 时，**只设置 `Authorization` header**，其余请求格式不变：

```http
POST /mcp/tools/call HTTP/1.1
Host: mcp.partner.internal
Authorization: Bearer {user_token 或 static token}
Content-Type: application/json

{ "name": "query_order", "arguments": {...} }
```

MCP server 侧零改造——它本来就在解析 `Authorization` 里的 token 做鉴权。

#### 4.4.3 为什么不传额外的用户信息 header

因为 token 里已经包含了 MCP server 需要的全部用户信息：
- JWT：MCP server 解析 claims 拿到 sub/role/dept
- opaque token：MCP server 调自己的 introspection 查

agent-flow 不需要也不应该转述用户信息——那是 MCP server 该自己解的事。

#### 4.4.4 实现位置

在 `backend/packages/harness/src/agent_flow_harness/mcp/loader.py` 的工具调用包装层，从 ContextVar 读取当前请求的 user_token（若有）：

```python
# 伪代码
def resolve_mcp_credential(connection):
    current_user_token = get_user_token_context()
    if current_user_token:
        return ("bearer_token", current_user_token)
    return (connection.auth_type, connection.auth_config.get("token"))
```

**关键**：MCP 工具实例的缓存 key 仍按 connection（`frozenset(config.name)`），**不加入 user 维度**。token 通过 ContextVar 在调用时动态读取，不烘进工具闭包。

ContextVar 新增（参考现有 `SandboxContext` / `WorkspaceContext` 范式）：

```python
# backend/packages/harness/src/agent_flow_harness/context/user_token.py
_current_user_token: ContextVar[str | None] = ContextVar("user_token", default=None)

def set_user_token_context(token: str | None) -> None: ...
def get_user_token_context() -> str | None: ...
def clear_user_token_context() -> None: ...
```

> token 仅在 ContextVar 生命周期内（单次请求）存在，**不写入 Redis、不写日志、不入库**。

注入点：`backend/app/engine/harness_integration/context.py` 的 `resolve_harness_context`（紧邻现有 MCP 注入逻辑 `:167-190`），新增 `set_user_token_context(resolved_user.user_token)`。

### 4.5 调用日志与 Token 统计

#### 4.5.1 目标

支撑三类需求（**不引入 EndUser 主数据表**，用事件流水表解决）：
1. **调用日志**：排查问题、审计"谁在什么时候调了什么"
2. **用户级 Token 消耗**：看某个终端用户花了多少 token、调了多少次
3. **API Key 汇总**：统计某 API Key 的总 token 消耗、活跃用户数、按 endpoint 分布

#### 4.5.2 现状与差距

| 能力 | 现状 | 差距 |
|------|------|------|
| API Key 调用计数 | `ExtApiStatsMiddleware` 写 Redis Hash（30 天 TTL） | 只是聚合计数，无明细，无 token，无用户维度 |
| Token 消耗 | 散落在 `sessions.total_tokens` / `messages.token_usage` / `tasks.total_tokens` | **token 与 api_key_id / user_sub 完全没关联** |
| Agent 路径的外部标记 | 只 workflow 路径的 task 有 `ext_api_key_id`；agent 路径的 session/message 没有 | 需要把 `api_key_id`/`user_sub` 一路透传到 execution service |
| 调用明细日志 | 无 | 需新建 |

#### 4.5.3 新增 collection：`ext_api_call_logs`

**性质**：事件流水表（一行 = 一次外部调用），不是用户主数据表。

```python
# backend/app/models/ext_api_call_log.py
class ExtApiCallLog(BaseModel):
    """外部 API 调用明细日志(事件流水)。"""

    id: str = Field(default_factory=lambda: generate_id("elog"), alias="_id")

    # —— 调用身份 ——
    api_key_id: str
    owner_user_id: str
    user_sub: str = ""              # 回调验证模式有值
    visitor_id: str = ""            # 兼容模式有值
    auth_mode: str = "legacy"       # callback | legacy

    # —— 调用上下文 ——
    endpoint: str = ""              # agents:invoke | workflows:invoke | ...
    agent_id: str = ""
    workflow_id: str = ""
    session_id: str = ""
    task_id: str = ""
    request_id: str = ""

    # —— 调用结果 ——
    status: str = "success"         # success | error
    status_code: int = 0
    error_code: str = ""
    latency_ms: int = 0

    # —— Token 消耗 (agent 执行结束后回填;workflow 路径为 0) ——
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0

    # —— BSON datetime (TTL 索引要求,不能用 ISO 字符串) ——
    timestamp: datetime = Field(default_factory=utc_now)
```

**MongoDB 索引**（由 `ExtApiCallLogService.ensure_indexes()` 创建，在 `main.py` lifespan 注册）：
- `(api_key_id, timestamp desc)` — 按接入方查时间序列
- `(api_key_id, user_sub, timestamp desc)` — 按用户查（回调验证模式）
- `(api_key_id, visitor_id, timestamp desc)` — 按设备查（兼容模式）
- `request_id` — 链路追踪
- `timestamp` 上的 **TTL 索引（90 天过期）** — 自动清理

> **TTL 注意事项**：`timestamp` 字段必须存 BSON datetime（不能用 ISO 字符串），否则 TTL 监控线程不会过期文档。这是项目里首个 TTL 索引。

#### 4.5.4 两阶段写入（最终实现）

**问题**：token 消耗在 agent 执行结束才知道，但请求开始时认证依赖就已经触发。

**解决**：用一个 request-scoped ContextVar（`ExtCallContext`）做"上下文中转站"，三段式写入：

```
阶段 1 — auth_and_rate_limit 依赖 (请求开始)
  - 从 principal 解析出 api_key_id / owner_user_id / user_sub /
    auth_mode / endpoint / request_id / start_time
  - set_ext_call_context(ExtCallContext(...)) stash 到 ContextVar
  - 不写库 (token 未知)

阶段 2 — AgentExecutionService 的 invoke 末尾 / _run() finally 块
  - _record_ext_call_log(agent_id, session_id, token_usage, error)
  - 从 ContextVar 读 ExtCallContext, patch agent_id/session_id
  - insert_one 一条完整 call log (含 token)
  - ctx.consumed = True  ← 标记已消费,中间件兜底跳过

兜底阶段 — ExtApiStatsMiddleware 响应阶段
  - 检查 ContextVar: 若 ctx 存在且 consumed=False
    (说明阶段 2 没执行,如 401/403/500 错误路径)
  - 写一条 token=0 的记录,保证失败调用也审计
  - ctx.consumed = True
```

**为什么用 ContextVar 而非 request.state**：
- `asyncio.create_task` 会自动复制 contextvars，所以 stream/resume 的后台任务能读到 stash 的上下文
- ContextVar 在 service 层（`AgentExecutionService`）可直接读，不用把 `api_key_id`/`user_sub` 一路透传方法签名

**Workflow 路径不写 call log**（重要简化）：
- workflow 在 Celery worker 跑，跨进程，ContextVar 不传播
- task 自己有 `total_tokens` 字段累计，通过 `source_session_id` 关联回会话（见 §4.5.8）
- workflow 的 `/ext/workflows/{id}/invoke` 调用本身会走阶段 1 + 兜底（写一条 token=0 的记录），但 task 内部 agent 节点的 token 不重复写到 call log

**错误路径的处理**（阶段 1 后请求失败、没到阶段 2）：
- 由 `ExtApiStatsMiddleware` 兜底：检查 `ctx.consumed`，若为 False 就写一条 token=0 的记录。保证 401/403/500 等失败调用也有日志。

#### 4.5.5 关键改造点

| 位置 | 改动 |
|------|------|
| `app/services/ext_api_call_log_service.py`（新建） | `ExtCallContext` dataclass + ContextVar 三函数 + `ExtApiCallLogService`（write_log / list_logs / get_token_summary / get_users_summary / ensure_indexes） |
| `app/api/v1/ext/__init__.py` `auth_and_rate_limit` | 阶段 1：解析身份 + set_ext_call_context |
| `app/api/v1/ext/__init__.py` `ExtApiStatsMiddleware` | 兜底：检查 consumed，未消费就写 token=0 记录 |
| `app/services/agent_execution_service.py` | 阶段 2：新增 `_record_ext_call_log` helper，在 invoke 末尾 / stream 和 resume 的 `_run()` finally 调用 |
| `app/main.py` lifespan | 注册 `ExtApiCallLogService.ensure_indexes()` |

**不需要改的**：
- `AgentExecutionService.invoke/stream/resume` 签名不变（用 ContextVar 读上下文，不透传参数）
- `engine/workflow/engine.py` 不动（workflow token 走 task 表）
- Task 模型不变（`source_session_id` 通过 `ext_metadata` merge）

#### 4.5.6 查询能力

有了这张表，可以支撑：

```python
# 1. 某 API Key 的总 token 消耗(任意时间段)
db.ext_api_call_logs.aggregate([
    {"$match": {"api_key_id": X, "timestamp": {"$gte": T1, "$lt": T2}}},
    {"$group": {"_id": None, "total_tokens": {"$sum": "$total_tokens"},
                "calls": {"$sum": 1}}}
])

# 2. 某用户的调用历史 / token 消耗(回调验证模式)
{"api_key_id": X, "user_sub": "user-123"}

# 3. 某 API Key 下的活跃用户列表(去重)
db.ext_api_call_logs.distinct("user_sub", {"api_key_id": X})

# 4. 某次请求的完整明细(排查问题)
{"request_id": "req_xxx"}

# 5. 按 endpoint 聚合 token 消耗
db.ext_api_call_logs.aggregate([
    {"$match": {"api_key_id": X}},
    {"$group": {"_id": "$endpoint", "tokens": {"$sum": "$total_tokens"}}}
])
```

#### 4.5.7 配套 API 端点

扩展现有 `/api/v1/api-keys/{id}/stats`（JWT + ADMIN，已有）：

```python
# 扩展返回(增加 token 维度)
GET /api/v1/api-keys/{id}/stats?start=...&end=...
{
  "api_key_id": "...",
  "total_requests": 1234,         # 现有 (Redis 聚合)
  "successful": 1200,             # 现有
  "failed": 34,                   # 现有
  "total_tokens": 456789,         # 新增 (ext_api_call_logs 聚合)
  "input_tokens": 300000,         # 新增
  "output_tokens": 156789,        # 新增
  "unique_users": 42,             # 新增 (近 30 天去重 user_sub 数)
  "by_endpoint": {...},           # 现有
  "last_used_at": "..."           # 现有
}

# 新增:调用明细查询
GET /api/v1/api-keys/{id}/logs?user_sub=...&visitor_id=...&session_id=...&endpoint=...&start=...&end=...&page=1&page_size=20
→ ApiKeyLogsResponse { items: [ApiKeyLogItem], total, page, page_size }

# 新增:用户列表(该 API Key 下的活跃用户,按 token 排序)
GET /api/v1/api-keys/{id}/users?period_days=7
→ ApiKeyUsersResponse { items: [{user_sub, calls, total_tokens, last_seen_at}], period_days }
```

#### 4.5.8 Task 关联：会话级总消耗

**问题**：agent 触发 workflow 后，task 在 Celery worker 跑（跨进程），ContextVar 不传播。如果要把 task 的 token 也写到 call log，需要侵入 engine.py 的完成钩子，复杂且跨进程。

**简化方案**：task 不写 call log，而是通过 `source_session_id` 字段关联回发起会话。查询时 join 两张表：

```
会话总消耗 = sum(ext_api_call_logs.total_tokens where session_id=X)
           + sum(tasks.total_tokens where source_session_id=X)
```

**实现**（`app/engine/agent/workflow_executor.py` 的 `dispatch_workflow`）：

```python
ws = _get_workspace()
source_session_id = getattr(ws, "session_id", "") or ""

# source_session_id 通过 ext_metadata merge 到 task 文档顶层
# (不改 Task 模型,复用现有 ext_metadata 机制)
ext_meta = {"source_session_id": source_session_id} if source_session_id else None
doc = await TaskService.create_task(..., ext_metadata=ext_meta)
```

**为什么用 ext_metadata 而非 Task 模型字段**：
- `ext_metadata` 是已有的通用 merge 机制（`task_service.py:132-134` 把它 update 到 doc 顶层）
- 避免改 Task 模型 + schema + 迁移
- `source_session_id` 只在 agent 触发的 task 上有意义，不是所有 task 都有

---

## 5. 安全与风控

### 5.1 防伪造：两层威胁对照

| 威胁 | 责任方 | 防护手段 |
|------|--------|----------|
| A. 接入方自己作弊 | 接入方（合同） | 审计日志、配额风控、合同 SLA |
| B1. 外部攻击者伪造 API Key | agent-flow | bcrypt hash + key_prefix 缓存校验（现有） |
| B2. 攻击者伪造 user_token | 接入方 + agent-flow | 接入方 introspection 校验 + Redis 缓存校验 |
| B3. 攻击者伪造调用 MCP server | MCP server | MCP server 自身的鉴权（解析 token；static 模式还有 auth_config） |
| B4. 接入方 introspection 被攻破 | 接入方 | 接入方自治，agent-flow 无法防 |

**关于 B3**：MCP 调用走 token 透传（回调验证模式）或 static token（兼容模式），MCP server 看到的就是它本就认识的 `Authorization`。伪造 MCP 调用的攻击者必须先有合法 token 或合法 static 凭证，这正是 MCP server 自身鉴权要防的事。

### 5.2 密钥与凭证管理

| 凭证 | 存储位置 | 加密 |
|------|----------|------|
| API Key 原文 | 永不存储（仅 bcrypt hash） | — |
| user_token | 仅 ContextVar 内存生命周期 | 不存储 |
| MCP server static 凭证 | `McpConnection.auth_config` | AES-256（现有） |
| Redis introspection 缓存 | Redis | 只存 sha256(token) 作 key + 非敏感结果 |

**user_token 不入库**：仅在请求处理期间存在于 ContextVar 内存中，请求结束即释放。Redis 缓存的是 introspection 结果（sub/attrs），不含原始 token。

### 5.3 审计日志

所有 `/ext/*` 调用（两种模式都记）写入 `ext_api_call_logs` collection，含完整调用身份、上下文、结果和 token 消耗。详见 §4.5。

关键审计能力：
- 按 `request_id` 查单次调用完整链路（排查问题）
- 按 `api_key_id` + 时间段查所有调用（接入方审计）
- 按 `user_sub` 查某用户的全部活动（用户审计，回调验证模式）
- token 消耗按多维度聚合（API Key / 用户 / endpoint / 时间）

明细保留 **90 天**（TTL 索引自动过期），过期前可由运营导出归档。

---

## 6. 落地计划

### 6.1 分阶段实施

| Phase | 范围 | 状态 | 验收 |
|-------|------|------|------|
| **P0: 契约与文档** | 本文档定稿 | ✅ 完成 | 团队 review 通过 |
| **P1: 核心链路** | ApiKey 加 `user_info_url` 字段、UserAuthService、introspection 缓存、`get_api_key_principal` 改造、ext 路由 user_id 解析 | ✅ 完成 (commit 4adab13) | 配 url 的 API Key 走 introspection；没配的行为不变 |
| **P2: MCP token 透传** | harness 层 `user_token_context` ContextVar、loader `_user_token_interceptor`、principal.user_token 透传链路 | ✅ 完成 (commit 4cbb1af) | 回调验证模式 MCP 收到 user_token；兼容模式不变 |
| **P3: 调用日志与 Token 统计** | `ext_api_call_logs` collection、两阶段写入（ContextVar + 兜底）、`source_session_id` task 关联、stats/logs/users 端点 | ✅ 完成 (commit 76459d7) | 能查 API Key 总 token、能按 user_sub 查历史、失败调用也有日志 |
| **前端 UI** | API Key 创建/编辑 Modal 支持所有字段（含 user_info_url）、列表显示模式 Tag | ✅ 完成 (commit 54061d7) | 运营人员能在后台配置 introspection URL |
| **P4: 接入方 SDK / 文档** | 接入方自查清单、introspection 参考实现（Python/Node） | ⏳ 待做 | 至少 1 个真实接入方跑通 |

### 6.2 兼容期设计

P1-P3 期间，新老逻辑并存：
- 存量 API Key（`user_info_url` 为空）→ 走 visitor_id（现有行为）
- 新 API Key（填了 url）→ 走 introspection

接入方升级路径：在 API Key 配置里填上 introspection URL + 前端把 `visitor_id` 改成 `X-User-Token`。

`ext_api_call_logs` 对两种模式都记录（`auth_mode` 字段区分），所以统计和审计能力覆盖全部调用，不因模式不同而割裂。

### 6.3 回滚预案

| 风险 | 回滚动作 |
|------|----------|
| introspection 服务大面积故障 | 接入方清空 API Key 的 `user_info_url` 回退到 visitor_id |
| MCP 透传 token 导致 MCP 调用失败 | 接入方清空 `user_info_url`，MCP 回退到 static 凭证 |

---

## 附录

### A. 完整请求/响应示例

#### A.1 接入方调用 agent（回调验证模式）

```http
POST /api/v1/ext/agents/agent_01HX/invoke HTTP/1.1
Host: api.agent-flow.com
Authorization: Bearer af_live_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
X-User-Token: Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEy...
Content-Type: application/json

{
  "message": "帮我查一下订单",
  "session_id": "sess_abc123"
}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Request-ID: req_xyz789

{
  "execution_id": "exec_01HX...",
  "session_id": "sess_abc123",
  "answer": "您的订单 #12345 已发货..."
}
```

#### A.2 agent-flow 调用接入方 introspection

```http
POST /oauth/introspect HTTP/1.1
Host: api.partner.com
Content-Type: application/x-www-form-urlencoded

token=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEy...
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "active": true,
  "sub": "user-12345",
  "username": "zhangsan",
  "email": "zhangsan@partner.com",
  "exp": 1735689600,
  "attrs": {
    "display_name": "张三",
    "dept": "销售一部"
  }
}
```

#### A.3 agent-flow 调 MCP server（回调验证模式）

```http
POST /mcp/tools/call HTTP/1.1
Host: mcp.partner.internal
Content-Type: application/json
Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEy...

{
  "name": "query_order",
  "arguments": {"order_id": "12345"}
}
```

MCP server 解析 token 拿到用户身份和权限，自己做鉴权决策。零自定义 header。

### B. 接入方自查清单

接入方对接 agent-flow 终端用户认证，需完成：

- [ ] **实现 introspection 端点**
  - [ ] POST 方法，`application/x-www-form-urlencoded`
  - [ ] 接收 `token` 字段
  - [ ] 返回 RFC 7662 格式 JSON（`active` 必需；active=true 时 `sub`/`exp` 必需）
  - [ ] token 无效时返回 `{"active": false}`（HTTP 200，不是 4xx）
  - [ ] 响应时间 < 500ms（避免触发 agent-flow 3 秒超时）

- [ ] **保证 sub 稳定**
  - [ ] 同一用户的每次 introspection 返回相同 `sub`
  - [ ] `sub` 不随 token 续签而变化

- [ ] **配置 API Key**
  - [ ] 在 agent-flow 后台，给用于外部调用的 API Key 填入 `user_info_url`

- [ ] **前端改造**
  - [ ] 用户登录后，调 agent-flow `/ext/*` 时同时传两个 header
  - [ ] 处理 401 `invalid_user_token`：刷新 token 或重新登录

### C. 常见问题

**Q1: 接入方没有 OAuth2 server，怎么实现 introspection？**

A: 写一个简单的 HTTP 端点即可：

```python
@app.post("/oauth/introspect")
async def introspect(token: str = Form(...)):
    user = await token_store.get(token)
    if not user:
        return {"active": False}
    return {
        "active": True,
        "sub": user.id,
        "username": user.username,
        "email": user.email,
        "exp": user.token_exp,
    }
```

**Q2: 接入方能否用自己的 token 格式（非 JWT）？**

A: 可以。introspection 的 `token` 字段对格式无要求，接入方完全可以传一个不透明的随机字符串（opaque token），由接入方自己的 introspection 端点去查缓存/DB 校验。这正是 RFC 7662 的典型用法。

**Q3: 用户切换设备后，历史会话还能看到吗？**

A: 只要接入方侧的 `sub` 不变（同一用户），切换设备后 agent-flow 解析出相同的 `f"{owner}:{sub}"`，历史会话完全可见。这解决了 visitor_id 不跨设备的痛点。

**Q4: introspection 缓存期间用户被撤销了怎么办？**

A: 最坏情况：缓存 TTL 内（60s）该用户仍能调用。这是开放平台的标准水平。如需更短窗口，可后续扩展撤销 webhook（当前不实现）。

**Q5: MCP server 怎么知道用户对应什么权限？**

A: MCP server 拿到的就是它本就认识的 token。它解析 token 拿到用户身份后，按自己已有的鉴权体系做决策（解析 JWT claims 做 RBAC，或调自己的 introspection，或查本地 ACL）。agent-flow 不掺和 MCP server 的权限决策。

**Q6: 为什么 introspection 不带调用方凭证？**

A: introspection 本质是"问一下 token 合不合法"，不是"返回敏感数据"。攻击者要查询必须先拿到 token，而拿到 token 的人本来就能问接入方"我是谁"——这是 token 的基本能力。所以接入方端点不需要校验调用方身份。

**Q7: 存量 API Key 会受影响吗？**

A: 不会。`user_info_url` 默认空，存量 API Key 走现有 visitor_id 逻辑，完全不变。只有主动填了 url 的 API Key 才启用回调验证模式。

---

## 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-07-21 | v0.1 | 初稿（注册制 + EndUser + JWT） |
| 2026-07-21 | v0.2 | 删除 MCP 调用的 HMAC 签名机制 |
| 2026-07-21 | v0.3 | MCP 调用改为 token 透传 + `McpConnection.auth_mode` |
| 2026-07-21 | v0.4 | 最终最小化版：删除 EndUser 模型、auth_mode、webhook、迁移脚本、配置项；ApiKey 仅加 `user_info_url` 一个字段；McpConnection 零改动 |
| 2026-07-21 | v0.5 | 新增 §4.5 调用日志与 Token 统计：`ext_api_call_logs` 流水表（一次调用一条），两阶段写入解决 token 延迟回填问题，90 天 TTL，stats/logs/users 端点扩展 |
| 2026-07-21 | v0.6 | 实施完成同步：§4.5 更新为最终实现（ContextVar `ExtCallContext` + `consumed` 标志的三段式写入，而非 request.state + log_id）；新增 §4.5.8 Task 关联（`source_session_id` 通过 ext_metadata merge，不侵入 Celery）；§6.1 标注 P1/P2/P3/前端已完成状态（含 commit hash） |

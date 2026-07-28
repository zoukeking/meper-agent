# 终端用户身份认证接入规范

> 对应能力：第三方接入方代表的真实终端用户（End-User）身份认证与会话隔离。
> 日期：2026-07-21
> 状态：方案已确认，待实施
> 关联文档：[`external-api-design.md`](./external-api-design.md)、[`implementation-artifacts/5-3-mcp-connection-management.md`](../implementation-artifacts/5-3-mcp-connection-management.md)

---

## 0. 文档目的与范围

本文档定义 **agent-flow 与第三方接入方之间关于"终端用户身份"的对接契约**，以及 agent-flow 内部为实现该契约所需的数据模型与改造点。

**适用读者**：
- 第三方接入方（实现 introspection 端点、调用 `/api/v1/ext/*`）
- agent-flow 后端开发者（实现 EndUser 模型、introspection 客户端、MCP 透传）
- agent-flow 自研 MCP server 开发者（按透传契约做权限隔离）

**不包含**：
- API Key 体系本身的设计（已在 `external-api-design.md` 定义）
- MCP 连接管理 CRUD（已在 5-3 story 定义）
- MCP server 内部的 ACL 实现（由各 server 自行决定）

---

## 1. 背景与核心问题

### 1.1 当前链路的缺口

agent-flow 目前的外部接入链路如下：

```
第三方接入方 ──[API Key]──> agent-flow ──[静态 auth_config]──> MCP server
                 │
                 └─ widget 传 visitor_id（浏览器 localStorage UUID）
                     └─ 后端拼成 {owner_user_id}:{visitor_id} 当 user_id
```

**缺口**：
1. **API Key 只代表"接入方"，不代表"接入方背后的真实人类"**。
2. **`visitor_id` 是前端生成的 UUID**，不可信、不持久、不跨设备、无任何用户属性。后端把它和 `owner_user_id` 拼成复合 user_id（`backend/app/api/v1/ext/agents.py:150`），仅能做会话隔离，无法支撑用户级权限决策。
3. **MCP server 完全拿不到调用者身份**。MCP connection 配置里只有静态 `auth_config`（token/api_key），所有用户共享同一个工具实例（`backend/packages/harness/src/agent_flow_harness/mcp/loader.py` 中 grep `user_id` 零命中），无法做 per-user 权限隔离。

### 1.2 两个身份维度的拆分

| 维度 | 解决的问题 | 变化频率 | 当前实现 | 本规范目标 |
|------|------------|----------|----------|------------|
| **接入方身份 (Principal)** | 谁在调 API？= 哪个第三方服务 | 长期稳定 | API Key + `owner_user_id` | 不变 |
| **终端用户身份 (End-User)** | 谁在使用？= 真实人类 | 每次请求带 | 缺失（用 visitor_id 凑合） | 新增 |

两者**正交**：同一个 API Key 下可以有任意多个终端用户。

### 1.3 信任模型分析

**核心问题**：如何防止接入方传的用户身份被伪造？

将"防伪造"拆成两个本质不同的威胁：

**威胁 A — 接入方自己作弊（声明了假用户）**
- 本质是**业务/合同问题，不是密码学问题**。接入方与其终端用户的关系发生在 agent-flow 边界之外。
- 技术只能做：留下不可否认证据、抬高作弊成本、事后追责。**JWT/签名在此解决"不可否认"，不解决"防作弊"本身**（密钥在接入方手里，接入方仍可签发任意用户的 token）。
- 对应手段：审计日志、配额风控、合同 SLA。

**威胁 B — 外部攻击者冒充（无 Key 的人伪造调用）**
- **这才是技术能真正防住的**。需要攻击者同时具备：有效 API Key（bcrypt hash）+ 归属该 Key 的 token + HTTPS + introspection 通过。

**因此本规范的设计原则**：
1. agent-flow 不做接入方用户系统的真实性背书——那是接入方自己的责任。
2. agent-flow 负责：可信地校验接入方声明的 token、生成稳定的内部标识、把身份信息可信地透传到下游 MCP。
3. 开放平台场景下，**审计 + 配额 + 不可否认** 是标准打法（参考 Stripe / OpenAI / 飞书开放平台）。

---

## 2. 架构总览

### 2.1 整体流程

```
┌──────────────┐
│ 终端用户浏览器│  (接入方前端)
└──────┬───────┘
       │ ① 登录接入方系统,拿到 user_token (接入方/MCP 共用的同一身份体系)
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
│  b. 从 X-User-Token 取 user_token                         │
│  c. 查 Redis 缓存 introspect:{key_id}:{sha256(token)}     │
│       ├─ 命中 → 直接用缓存结果                             │
│       └─ 未命中 → 回调接入方 /introspect (RFC 7662)        │
│  d. upsert EndUser by (api_key_id, sub) → 拿到 end_user_id│
│  e. 调 agent / MCP 时按 MCP connection 的 auth_mode 取凭证 │
└──────┬───────────────────────────────────────────────────┘
       │ ③ 调 MCP server
       ↓
┌──────────────────────────────────────────────────────────┐
│ MCP server (自研,与接入方同一身份体系)                     │
│   Authorization: Bearer {user_token 或 static token}      │
│   ↓ 用自己的鉴权体系解析 token,做权限决策                  │
└──────────────────────────────────────────────────────────┘
```

**两类 MCP connection**（§4.1.3）：

| auth_mode | 凭证来源 | 适用场景 |
|-----------|----------|----------|
| `static`（默认） | `auth_config` 里的静态凭证 | 平台用户测试、discover/test_connection、公共 MCP |
| `user_token` | 当前请求的 `X-User-Token` | 外部接入方调用（MCP server 与接入方同一身份体系） |

### 2.2 三层模型

```
┌─────────────────────────────────────────────────────────┐
│ 认证层（每请求）                                         │
│   API Key (接入方身份) + X-User-Token (终端用户身份)     │
│   → introspect 拿到接入方侧 sub + attrs                  │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 身份层（稳定）                                            │
│   EndUser { id: end_01HX..., api_key_id, sub, attrs }    │
│   唯一键: (api_key_id, sub) — 跨 token / 跨设备稳定      │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 会话/数据层                                               │
│   session / file / audit 都挂 end_user_id                │
│   (替换原 {owner_user_id}:{visitor_id} 复合标识)          │
└─────────────────────────────────────────────────────────┘
```

### 2.3 与现有 API Key 体系的关系

| 层 | 既有 | 本规范新增 |
|----|------|------------|
| 接入方身份 | `ApiKey` 模型 + `ApiKeyPrincipal` + `get_api_key_principal` | ApiKey 新增 `user_auth` 字段（配置 introspection） |
| 终端用户身份 | — | `EndUser` 模型 + `EndUserService` + `get_end_user` Depends |
| 会话归属 | `{owner}:{visitor_id}` 复合 user_id | `end_user_id` |
| MCP 身份 | 静态 `auth_config`（所有调用共用） | `auth_mode` 区分：static 用 `auth_config`，user_token 透传用户 token |

**向后兼容**：visitor_id 不废弃，作为"匿名 EndUser 的 sub"走同一套逻辑（见 §5.2）。

---

## 3. 接入方对接规范（对外契约）

### 3.1 双凭证请求格式

所有 `/api/v1/ext/*` 端点支持（且仅支持）以下请求头：

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
- `Authorization`：必须是 agent-flow 颁发的 API Key（`af_live_` 前缀）。
- `X-User-Token`：接入方自己颁发的用户 token。agent-flow 会回调接入方校验，**不在本地存储原始 token**。

两个凭证**正交**，互不干扰。

**为什么不用同一个 `Authorization`**：两者是不同维度的凭证。API Key 是"接入方是谁"，User Token 是"接入方代谁调用"。复用会导致语义冲突、调试困难、无法区分。

**匿名调用**：当 API Key 配置了 `allow_anonymous: true` 时，可省略 `X-User-Token`（见 §5.2）。

### 3.2 Introspection 接口规范（RFC 7662 子集）

接入方在创建 API Key 时，向 agent-flow 提供以下配置：

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `user_info_url` | 接入方的 introspection 端点 URL | `https://api.partner.com/oauth/introspect` |
| `user_info_auth_type` | agent-flow 调用该端点的鉴权方式 | `basic` / `bearer` / `api_key` |
| `user_info_credential` | 调用凭证（AES-256 加密存储） | `client_id:client_secret` 或 token |
| `allow_anonymous` | 是否允许匿名调用（缺 X-User-Token 时） | `false`（默认） |

#### 3.2.1 请求格式（agent-flow → 接入方）

```http
POST /oauth/introspect HTTP/1.1
Host: api.partner.com
Content-Type: application/x-www-form-urlencoded
Authorization: Basic {base64(client_id:client_secret)}

token={接入方用户 token，URL-encoded}
```

**约定**：
- 方法必须为 `POST`
- `Content-Type` 必须为 `application/x-www-form-urlencoded`
- 请求体仅含一个字段 `token`（URL-encoded）
- 鉴权头由 `user_info_auth_type` 决定：
  - `basic`：`Authorization: Basic {base64(credential)}`
  - `bearer`：`Authorization: Bearer {credential}`
  - `api_key`：自定义头 `X-Api-Key: {credential}`（约定字段名）

#### 3.2.2 成功响应（HTTP 200）

```json
{
  "active": true,
  "sub": "user-12345",
  "username": "zhangsan",
  "email": "zhangsan@partner.com",
  "scope": "agent:invoke",
  "exp": 1735689600,
  "attrs": {
    "display_name": "张三",
    "dept": "销售一部",
    "role": "manager"
  }
}
```

**字段约束**：

| 字段 | 必需 | 类型 | 说明 |
|------|------|------|------|
| `active` | ✅ | bool | token 是否有效 |
| `sub` | ✅（active=true 时） | string | 接入方侧稳定用户 ID（**不随 token 变化**），最长 128 字符 |
| `exp` | ✅（active=true 时） | int | token 过期时间，Unix 时间戳（秒） |
| `username` | 推荐 | string | 登录名，用于展示 |
| `email` | 推荐 | string | 邮箱，用于展示 |
| `scope` | 可选 | string | 空格分隔的权限范围 |
| `attrs` | 可选 | object | 业务属性，原样透传到 MCP |

**关键约束**：
- `sub` 必须稳定（同一用户每次 introspect 返回相同的 `sub`），否则会话归属会断裂。
- `active: false` 时只需返回 `{"active": false}`，其余字段可省略。

#### 3.2.3 失败响应

**RFC 7662 规定：token 无效时仍返回 HTTP 200 + `{"active": false}`**，不要返回 4xx。

| HTTP | 含义 | agent-flow 处理 |
|------|------|-----------------|
| 200 + `{"active": false}` | token 无效/过期/被撤销 | 返回 401 `invalid_user_token` |
| 200 + `{"active": true, ...}` | 有效 | upsert EndUser，继续处理 |
| 401 / 403 | agent-flow 调用接入方的凭证失效 | 返回 503 `user_service_misconfigured`，告警 |
| 4xx（非 401/403） | 接入方端点异常 | 返回 503 `user_service_error` |
| 5xx / 超时 | 接入方不可用 | 见 §3.3 降级策略 |

#### 3.2.4 为什么用 RFC 7662

- **接入方有 OAuth2/OIDC server**（Auth0 / Keycloak / Authing / Casdoor 等）→ 端点开箱即用，零开发成本。
- **接入方没有 OAuth2 server** → 实现一个标准端点（接收 token、查缓存/DB、返回 JSON），有大量开源参考。
- **工具链成熟**：标准请求格式，可用 curl/postman 直接调试，无需特殊 SDK。

### 3.3 错误码与降级策略

#### 3.3.1 对接入方的错误响应

| 场景 | HTTP | code | 接入方前端处理 |
|------|------|------|----------------|
| `X-User-Token` 缺失（且未允许匿名） | 401 | `missing_user_token` | 引导用户登录接入方 |
| introspect 返回 `active: false` | 401 | `invalid_user_token` | token 失效，重新登录 |
| introspect 接口 401/403 | 503 | `user_service_misconfigured` | 联系 agent-flow 平台方（接入方配置错误） |
| introspect 接口 4xx/5xx（首次，无缓存） | 503 | `user_service_unavailable` | 提示"用户服务暂不可用，请稍后重试" |
| introspect 接口超时（首次，无缓存） | 504 | `user_service_timeout` | 同上 |
| EndUser upsert 失败 | 500 | `internal_error` | 重试 |

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

#### 3.3.2 降级策略（introspect 超时/5xx 但 Redis 有缓存）

**核心原则**：缓存命中时，即使接入方 introspection 服务异常，也尽量**降级服务**而非直接失败。

```
introspect 调用失败时:
  if Redis 有该 token 的缓存（未过期）:
      使用缓存结果继续，响应头加 X-User-Auth-Stale: true
      异步告警（不阻塞请求）
  else:
      按错误码表返回 503/504
```

**TTL 限制**：降级使用的缓存 TTL 上限为配置 TTL（默认 60s），不延长。超过则视为失效。

#### 3.3.3 重试约定

agent-flow 对 introspection 的调用：
- 超时默认 3 秒（可在 API Key 的 `user_auth.timeout` 配置，1-10s）
- 不重试（避免雪崩放大接入方压力）
- 接入方 introspection 接口本身应保证幂等（同一 token 多次 introspect 结果一致）

### 3.4 撤销通知 Webhook（可选，推荐）

接入方撤销用户后，introspection 结果的 TTL（默认 60s）内有窗口期——被撤销的用户仍可能命中缓存。为缩短该窗口，接入方**可选**实现撤销通知：

```http
POST /api/v1/ext/webhooks/user-revoked HTTP/1.1
Host: api.agent-flow.com
Authorization: Bearer af_live_xxx

{
  "sub": "user-12345",
  "revoked_at": "2026-07-21T10:00:00Z"
}
```

**鉴权**：复用 API Key（`Authorization: Bearer af_live_xxx`），通过现有的 `auth_and_rate_limit` 校验。无需额外签名——API Key 本身已经证明调用方身份。

agent-flow 收到后：清除该接入方该 `sub` 的所有 introspection 缓存 + 标记对应 EndUser 为 `disabled`。

**不实现此 webhook 不影响功能正确性**，只是撤销生效时间从"立即"延长到"最多 TTL"。

---

## 4. agent-flow 侧实现规范（对内契约）

### 4.1 数据模型

#### 4.1.1 ApiKey 扩展

在现有 `ApiKey` 模型（`backend/app/models/api_key.py`）新增 `user_auth` 字段：

```python
class ApiKeyUserAuth(BaseModel):
    """API Key 绑定的终端用户认证配置。"""

    # 是否启用终端用户认证。False 时回退到 visitor_id 匿名模式（兼容现有 widget）。
    enabled: bool = Field(default=False)

    # 接入方 introspection 端点（RFC 7662）
    user_info_url: str = Field(default="", max_length=500)
    user_info_auth_type: str = Field(default="basic")  # basic | bearer | api_key
    user_info_credential: str = Field(
        default="",
        description="调用 introspection 端点的凭证，AES-256 加密存储",
    )

    # 是否允许匿名调用（缺 X-User-Token 时退化为 visitor_id）
    allow_anonymous: bool = Field(default=False)

    # introspection 超时（秒）
    timeout: int = Field(default=3, ge=1, le=10)

    # introspection 缓存 TTL（秒）
    cache_ttl: int = Field(default=60, ge=5, le=600)
```

`ApiKey.user_auth: ApiKeyUserAuth` 默认 `enabled=False`，保证现有 API Key 行为不变。

#### 4.1.2 EndUser 模型（新增）

`backend/app/models/end_user.py`：

```python
class EndUserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class EndUser(BaseModel):
    """终端用户(agent-flow 侧稳定标识)。

    不是身份源 —— 身份仍归接入方。本模型的作用是:
    1. 提供跨 token / 跨设备稳定的 end_user_id (会话归属)
    2. 缓存最近一次 introspection 拿到的属性快照
    3. 作为 MCP 透传的稳定标识
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("end"), alias="_id")
    api_key_id: str = Field(..., description="归属的 API Key")
    sub: str = Field(..., max_length=128, description="接入方侧用户 ID")
    is_anonymous: bool = Field(default=False, description="匿名接入(visitor_id 退化)")

    # 属性快照(最近一次 introspection 的结果)
    username: str = Field(default="")
    email: str = Field(default="")
    attrs: dict = Field(default_factory=dict)

    display_name: str = Field(default="", description="展示名(优先 attrs.display_name)")

    status: EndUserStatus = Field(default=EndUserStatus.ACTIVE)
    first_seen_at: str = Field(default_factory=lambda: utc_now().isoformat())
    last_seen_at: str = Field(default_factory=lambda: utc_now().isoformat())
```

**MongoDB 索引**（`backend/app/services/end_user_service.py` 初始化）：
- 唯一索引：`(api_key_id, sub)` 升序
- 普通索引：`api_key_id`
- 普通索引：`status`

**关键约束**：
- `(api_key_id, sub)` 唯一，保证同一接入方同一用户永远命中同一条记录。
- 不同 API Key 即使 `sub` 相同，也是不同的 EndUser（用 `api_key_id` 隔离）。
- 匿名 EndUser 的 `sub` 形如 `anon:{visitor_id}`，`is_anonymous=true`。

#### 4.1.3 McpConnection 扩展

在现有 `McpConnection` 模型（`backend/app/models/mcp_connection.py`）新增 `auth_mode` 字段：

```python
class McpAuthMode(StrEnum):
    """MCP server 调用时凭证的来源。"""

    STATIC = "static"        # 始终用 auth_config 里的静态凭证(默认,向后兼容)
    USER_TOKEN = "user_token"  # 透传当前请求的 X-User-Token


class McpConnection(BaseModel):
    # ... 现有字段不变 ...

    auth_mode: McpAuthMode = Field(
        default=McpAuthMode.STATIC,
        description="凭证来源:static 用 auth_config;user_token 透传用户 token",
    )
```

**运行时凭证解析逻辑**：

```
调 MCP server 前:
  if connection.auth_mode == "user_token":
      # 外部用户场景:必须有 X-User-Token
      if 当前调用是 ext 请求且没有 user_token:
          raise 401 missing_user_token
      if 当前调用是平台内部请求(JWT):
          raise 400 mcp_requires_external_user   # 配置错误
      credential = 当前请求的 user_token
      auth_type = "bearer_token"   # user_token 一定是 Bearer
  else:  # static
      credential = connection.auth_config 的静态凭证   # 现有逻辑
      auth_type = connection.auth_type
```

**边界 case 处理**：

| 场景 | 处理 |
|------|------|
| `auth_mode = "user_token"` + ext 请求带 token | ✅ 透传 user_token |
| `auth_mode = "user_token"` + ext 请求无 token | ❌ 401 `missing_user_token`（外部用户必须带） |
| `auth_mode = "user_token"` + 平台用户调用 | ❌ 400 `mcp_requires_external_user`（配置与调用方不匹配，建议该 connection 改用 static） |
| `auth_mode = "static"` + 任何调用方 | ✅ 用 `auth_config`（现有行为，完全不变） |

**使用建议**：
- 平台内部测试、`discover_tools`、`test_connection` 始终用 `static`（这俩操作本来就在管理后台走 JWT，没有用户 token）。
- 给外部接入方用的 MCP connection 设 `user_token`，并在描述里注明"该 MCP 与接入方共用同一身份体系"。
- **前提**：`user_token` 模式要求 MCP server 能识别接入方的 token（同一 SSO / MCP 由接入方提供 / MCP 对接了接入方的 introspection）。若不满足，应使用 static 或另议。

### 4.2 Introspection 客户端

`backend/app/services/user_auth_service.py`（新增）：

```python
class UserAuthService:
    """调用接入方 introspection 端点 + Redis 缓存。"""

    INTROSPECT_CACHE_PREFIX = "introspect"
    CACHE_TTL_DEFAULT = 60

    async def introspect(
        self,
        api_key: ApiKey,
        user_token: str,
    ) -> IntrospectionResult:
        """
        返回 introspection 结果。带 Redis 缓存。

        失败降级策略见 §3.3.2。
        """
        cache_key = self._cache_key(api_key.id, user_token)
        cached = await self._get_cached(cache_key)
        if cached and not cached.expired:
            return cached

        try:
            result = await self._call_introspect(api_key.user_auth, user_token)
        except (TimeoutError, ServiceError) as e:
            # 降级:用 stale 缓存
            if cached:
                return cached.as_stale()
            raise

        await self._set_cache(cache_key, result, ttl=api_key.user_auth.cache_ttl)
        return result
```

**缓存 key**：
```python
def _cache_key(self, api_key_id: str, user_token: str) -> str:
    token_hash = sha256(user_token.encode()).hexdigest()
    return f"{self.INTROSPECT_CACHE_PREFIX}:{api_key_id}:{token_hash}"
```

**不缓存的内容**：原始 token 永不缓存（只缓存 hash + introspection 结果）。

### 4.3 稳定标识生成与映射

```python
async def resolve_end_user(
    api_key: ApiKey,
    user_token: str | None,
    visitor_id: str | None,
) -> EndUser:
    """
    解析出稳定的 EndUser。三种路径:

    A. user_token 存在 → introspect → upsert by (api_key_id, sub)
    B. user_token 缺失 + allow_anonymous → 用 visitor_id 当匿名 sub
    C. user_token 缺失 + 不允许匿名 → 抛 401 missing_user_token
    """
    if user_token:
        result = await user_auth_service.introspect(api_key, user_token)
        if not result.active:
            raise ExtAuthError("invalid_user_token")
        return await end_user_service.upsert_by_sub(
            api_key_id=api_key.id,
            sub=result.sub,
            attrs={...result},
        )

    if api_key.user_auth.allow_anonymous and visitor_id:
        return await end_user_service.upsert_by_sub(
            api_key_id=api_key.id,
            sub=f"anon:{visitor_id}",
            is_anonymous=True,
        )

    raise ExtAuthError("missing_user_token")
```

**`upsert_by_sub` 保证**：同一 `(api_key_id, sub)` 永远返回同一个 `end_user_id`，跨 token、跨设备、跨时间稳定。

### 4.4 MCP 调用时的凭证传递

#### 4.4.1 核心思路：token 透传，MCP 自治鉴权

MCP server **本身已有完整的鉴权体系**（解析 token、查权限、做决策）。agent-flow 调 MCP 时只需要一件事：**把正确的 token 放进 `Authorization` header**。MCP server 拿到它本就认识的 token，自然能完成鉴权和权限决策。

**不需要**：
- ❌ 自定义 `X-Agentflow-*` header 转述用户信息（冗余，MCP 从 token 自己拿）
- ❌ HMAC 签名/验签（职责越界，MCP 自身鉴权已足够）
- ❌ agent-flow 给 MCP server 定义权限契约（MCP 自己管自己的 ACL）

**关键前提**：MCP server 和接入方共用**同一身份体系**（同一 SSO、或 MCP 由接入方提供、或 MCP 对接了接入方的 token 校验能力）。在这个前提下，接入方的 user_token 对 MCP server 是原生可识别的。

#### 4.4.2 两类凭证来源（auth_mode）

见 §4.1.3。MCP connection 在创建时明确凭证来源：

```
auth_mode = "static"      → 用 auth_config 的静态 token (平台测试/公共 MCP)
auth_mode = "user_token"  → 透传当前请求的 X-User-Token (外部接入方调用)
```

agent-flow 调 MCP server 时，**只修改 `Authorization` header**，其余请求格式不变：

```http
POST /mcp/tools/call HTTP/1.1
Host: mcp.partner.internal
Authorization: Bearer {static token 或 user_token}
Content-Type: application/json

{ "name": "query_order", "arguments": {...} }
```

MCP server 侧零改造——它本来就在解析 `Authorization` 里的 token 做鉴权。

#### 4.4.3 为什么不传额外的用户信息 header

因为 token 里已经包含了 MCP server 需要的全部用户信息：
- 如果是 JWT：MCP server 解析 claims 拿到 sub/role/dept 等
- 如果是 opaque token：MCP server 调自己的 introspection 查
- 无论如何，**MCP server 知道的不会比 token 里能解出来的更多**——agent-flow 再额外传一份是重复

agent-flow 缓存的 introspection 结果（EndUser 的 sub/attrs）是给 agent-flow **自己**用的（会话归属、审计、限流），不需要也不应该塞给 MCP server——那是 MCP server 该自己解的事。

#### 4.4.4 实现位置

在 `backend/packages/harness/src/agent_flow_harness/mcp/loader.py` 的工具加载/调用包装层，从 ContextVar 读取当前请求的 user_token（若有），按 connection 的 `auth_mode` 决定凭证：

```python
# 伪代码
def resolve_mcp_credential(connection, current_user_token):
    if connection.auth_mode == McpAuthMode.USER_TOKEN:
        if not current_user_token:
            raise McpAuthError("missing_user_token")
        return ("bearer_token", current_user_token)
    else:  # static
        return (connection.auth_type, connection.auth_config["token"])
```

**关键**：MCP 工具实例的缓存 key 仍按 connection（`frozenset(config.name)`），**不加入 user 维度**。token 通过 ContextVar 在调用时动态读取，不烘进工具闭包——否则缓存膨胀且无法共享。

ContextVar 新增（参考现有 `SandboxContext` / `WorkspaceContext` 范式）：

```python
# backend/packages/harness/src/agent_flow_harness/context/end_user.py
@dataclass(frozen=True)
class EndUserContext:
    """当前请求的终端用户上下文(供 agent-flow 内部使用)。"""
    end_user_id: str          # agent-flow 颁发的稳定 ID(会话归属)
    sub: str                   # 接入方侧用户 ID(审计)
    api_key_id: str
    raw_token: str             # 原始 user_token(仅用于透传给 user_token 模式的 MCP)
    is_anonymous: bool

_current_end_user: ContextVar[EndUserContext | None] = ContextVar("end_user", default=None)

def set_end_user_context(ctx: EndUserContext) -> None: ...
def get_end_user_context() -> EndUserContext | None: ...
def clear_end_user_context() -> None: ...
```

> `raw_token` 仅在 ContextVar 生命周期内（单次请求）存在，**不写入 Redis、不写日志、不入库**。

注入点：`backend/app/engine/harness_integration/context.py` 的 `resolve_harness_context`（紧邻现有 MCP 注入逻辑 `:167-190`），新增 `set_end_user_context(...)`。

### 4.5 Ext 路由集成

`backend/app/api/v1/ext/__init__.py` 现有的 `auth_and_rate_limit` 之上，新增 `get_end_user` Depends：

```python
async def auth_and_resolve_user(
    request: Request,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> tuple[ApiKeyPrincipal, EndUser]:
    """
    组合依赖:校验 API Key + 解析 EndUser。
    替换 ext 路由里对 auth_and_rate_limit 的依赖。
    """
    api_key = await api_key_service.get_by_id(principal.key_id)
    user_token = _extract_user_token(request)  # 从 X-User-Token 提取
    visitor_id = _extract_visitor_id(request)  # 从 body/query 提取(兼容)
    end_user = await resolve_end_user(api_key, user_token, visitor_id)
    return principal, end_user
```

现有 ext 路由改造（`backend/app/api/v1/ext/agents.py`、`workflows.py` 等）：
- 删除手动拼接 `{owner_user_id}:{visitor_id}` 的代码（`:150`、`:186`、`:233`、`:284`、`:323`、`:368`）
- 改用 `end_user.id` 作为 `user_id` 传给内部 service

---

## 5. 会话与数据归属迁移

### 5.1 从 visitor_id 到 end_user_id 的迁移策略

**复合 user_id 模式必须废弃**。理由：
- `{owner_user_id}:{visitor_id}` 是临时凑合方案，会污染 session 归属、阻碍跨设备、无法支撑审计。
- 新 EndUser 体系上线后，应使用干净的 `end_user_id`。

**迁移分阶段进行**：

| 阶段 | 动作 | 兼容性 |
|------|------|--------|
| Phase 1（上线） | 新请求全部用 `end_user_id`；旧 session 仍按 `visitor_id` 归属 | 旧会话可见，新会话走新链路 |
| Phase 2（迁移脚本） | 把历史 session 按 `(owner_user_id, visitor_id)` 映射到对应 EndUser（匿名），批量改写 `session.user_id` | 历史会话归属到匿名 EndUser |
| Phase 3（接入方升级） | 接入方接入真实登录后，前端把 visitor_id 换成 user_token；新会话归属到真实 EndUser | 历史匿名会话和新的实名会话并存，接入方可自行做合并 |

**迁移脚本契约**（一次性，在 Phase 2 跑）：

```
输入: 所有 sessions where user_id matches "{owner_user_id}:{visitor_id}" 模式
处理:
  对每条 session:
    sub = "anon:{visitor_id}"
    end_user = upsert EndUser(api_key_id=?, sub=sub, is_anonymous=true)
    # 注意:历史 session 没记 api_key_id,需要从 owner_user_id 反查 owner 的所有 api_key
    # 若一个 owner 有多个 api_key,无法精确归属 → 归到最早创建的那个,打标记 needs_review
    session.user_id = end_user.id
输出: 迁移报告(N 条迁移成功 / N 条需要人工 review)
```

### 5.2 匿名接入的退化路径

当 `X-User-Token` 缺失但 API Key 配置了 `allow_anonymous: true` 时：

```
visitor_id 存在 → sub = "anon:{visitor_id}"
visitor_id 缺失 → sub = "anon:{随机 UUID}"(一次性)
upsert EndUser(api_key_id, sub, is_anonymous=true)
```

**对现有 widget 的兼容**：
- widget 现有逻辑（`agent-flow-widget/src/lib/visitor.ts`）继续生成 visitor_id，传给后端。
- 后端把 visitor_id 当作匿名 sub 走同一套 EndUser 逻辑。
- widget 不需要任何改动。
- 接入方未来接入真实登录后，前端把 visitor_id 换成 `X-User-Token`，链路无感切换。

**链路统一**：匿名和实名走完全相同的代码路径，仅 `is_anonymous` 标志不同。MCP server 可据此降级权限（如匿名用户只能读不能写）。

### 5.3 数据迁移脚本

`backend/app/scripts/migrate_visitor_to_enduser.py`（新增）：

```
用法: python -m app.scripts.migrate_visitor_to_enduser [--dry-run] [--api-key-id ID]
参数:
  --dry-run         只打印迁移计划,不写库
  --api-key-id ID   只迁移指定 API Key 的 session(默认全部)
输出:
  迁移报告 JSON: { total, migrated, needs_review, errors }
```

---

## 6. 安全与风控

### 6.1 防伪造：两层威胁对照

| 威胁 | 责任方 | 防护手段 |
|------|--------|----------|
| A. 接入方自己作弊 | 接入方（合同） | 审计日志、配额风控、合同 SLA、可选 webhook 不可否认 |
| B1. 外部攻击者伪造 API Key | agent-flow | bcrypt hash + key_prefix 缓存校验（现有） |
| B2. 攻击者枚举 end_user_id | agent-flow | ULID（26 字符，不可枚举） |
| B3. 攻击者伪造 user_token | 接入方 + agent-flow | 接入方 introspection 校验 + Redis 缓存校验 |
| B4. 攻击者伪造调用 MCP server | MCP server | MCP server 自身的鉴权（解析 token；static 模式还有 auth_config） |
| B5. 接入方 introspection 被攻破 | 接入方 | 接入方自治，agent-flow 无法防（属于接入方责任） |

**关于 B4**：因为 MCP 调用走 token 透传（user_token 模式）或 static token（static 模式），MCP server 看到的就是它本就认识的 `Authorization` header。伪造 MCP 调用的攻击者必须先有合法 token（user_token 场景）或合法 static 凭证（static 场景），这正是 MCP server 自身鉴权要防的事。agent-flow 不掺和。

### 6.2 密钥与凭证管理

| 密钥 | 存储位置 | 加密 | 轮换 |
|------|----------|------|------|
| API Key 原文 | 永不存储（仅 bcrypt hash） | — | 撤销重建 |
| `user_info_credential` | ApiKey 文档 | AES-256 | 更新 API Key 配置 |
| MCP server static 凭证 | `McpConnection.auth_config`（static 模式） | AES-256 | 更新 MCP connection（现有，不变） |
| user_token | 仅 ContextVar 内存生命周期 | 不存储 | 接入方自治 |

**所有敏感字段加密复用现有 `MODEL_ENCRYPTION_KEY`**（`backend/app/services/mcp_connection_service.py` 已有 `_SENSITIVE_AUTH_KEYS` 范式，扩展即可）。

**user_token 不入库**：仅在请求处理期间存在于 ContextVar 内存中，请求结束即释放。Redis 缓存的是 introspection 结果（sub/attrs），不含原始 token。

### 6.3 配额、限流、审计

#### 6.3.1 限流（现有 + 新增）

| 维度 | 限制 | 实现 |
|------|------|------|
| 每 API Key | 默认 60 次/分钟（现有） | Redis 滑动窗口 |
| 每端用户 | 可选，默认不限 | Redis 滑动窗口（key: `rate:end:{end_user_id}`） |
| introspection 调用频率 | 每 token 每 TTL 内最多 1 次（缓存保证） | Redis 缓存天然限流 |

#### 6.3.2 审计日志

所有 `/ext/*` 调用记录：

```
{
  "request_id": "...",
  "api_key_id": "...",
  "end_user_id": "...",
  "user_sub": "...",
  "endpoint": "agents:invoke",
  "agent_id": "...",
  "session_id": "...",
  "is_anonymous": false,
  "introspect_stale": false,      // 是否用了 stale 缓存
  "status": "success|error",
  "latency_ms": 123,
  "timestamp": "..."
}
```

写入现有 `ExtApiStatsMiddleware`（`backend/app/api/v1/ext/__init__.py:59-111`）扩展。

#### 6.3.3 异常告警

| 事件 | 告警 |
|------|------|
| introspection stale 缓存命中率 > 10% | 接入方 introspection 服务异常 |
| 单 end_user 1 分钟调用 > 100 | 可能滥用 |
| MCP server 鉴权失败率突增（MCP server 侧统计） | 可能伪造或凭证泄露 |

---

## 7. 落地计划

### 7.1 分阶段实施

| Phase | 范围 | 产出 | 验收 |
|-------|------|------|------|
| **P0: 契约与文档** | 本文档定稿、MCP auth_mode 协议对齐 | 本文档 | 团队 review 通过 |
| **P1: 核心链路（不破坏现有）** | EndUser 模型、UserAuthService、auth_and_resolve_user Depends、ContextVar 注入 | 新 API Key 配置 `user_auth.enabled=true` 时走 introspection；`enabled=false`（默认）走老逻辑 | 新接口跑通，现有 widget 行为不变 |
| **P2: MCP token 透传** | McpConnection 新增 `auth_mode` 字段、loader 按 auth_mode 取凭证 | user_token 模式 MCP 收到用户 token；static 模式行为不变 | 端到端：ext 调用 → MCP 收到正确 token |
| **P3: 迁移与兼容** | 数据迁移脚本、visitor_id 退化路径、审计扩展 | 历史 session 归属到匿名 EndUser；widget 行为不变 | 迁移报告通过 |
| **P4: 接入方 SDK / 文档** | 接入方自查清单、introspection 参考实现（Python/Node 各一） | 接入方可独立完成对接 | 至少 1 个真实接入方跑通 |

### 7.2 兼容期设计

**P1-P3 期间，新老逻辑并存**：
- API Key 未配置 `user_auth.enabled` → 走老逻辑（visitor_id 复合 user_id）
- API Key 配置 `user_auth.enabled=true` → 走新逻辑（introspection + EndUser）
- 两条路径都产出 `user_id`，下游 service 无感知

**P4 之后**：所有新接入方强制启用 `user_auth.enabled=true`；存量接入方逐步迁移。

### 7.3 回滚预案

| 风险 | 回滚动作 |
|------|----------|
| introspection 服务大面积故障 | 接入方配置 `user_auth.enabled=false` 回退到 visitor_id |
| EndUser upsert 出错 | 异常时降级用 `(api_key_id, sub)` 哈希作临时 user_id |
| MCP server 用户隔离逻辑异常 | MCP server 侧关闭按 header 的权限检查，退回全量放行 |
| 数据迁移出错 | 迁移脚本支持 `--dry-run`；session 集合做备份后再迁移 |

---

## 附录

### A. 完整请求/响应示例

#### A.1 接入方调用 agent（带用户 token）

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
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 59
X-RateLimit-Reset: 1735689660

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
Authorization: Basic YWdlbnQtZmxvdzpzZWNyZXQ=

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

#### A.3 agent-flow 调 MCP server

**场景 1：user_token 模式**（外部接入方调用）

```http
POST /mcp/tools/call HTTP/1.1
Host: mcp.partner.internal
Content-Type: application/json
Authorization: Bearer {接入方用户的 token,原样透传自 X-User-Token}

{
  "name": "query_order",
  "arguments": {"order_id": "12345"}
}
```

MCP server 解析 token 拿到用户身份和权限，自己做鉴权决策。零自定义 header。

**场景 2：static 模式**（平台用户测试）

```http
POST /mcp/tools/call HTTP/1.1
Host: mcp.partner.internal
Content-Type: application/json
Authorization: Bearer {mcp_connection.auth_config 里的 static token}

{
  "name": "query_order",
  "arguments": {"order_id": "12345"}
}
```

完全复用现有行为，与本次改造无关。

### B. 接入方自查清单

接入方对接 agent-flow 终端用户认证，需完成：

- [ ] **实现 introspection 端点**
  - [ ] POST 方法，`application/x-www-form-urlencoded`
  - [ ] 接收 `token` 字段
  - [ ] 返回 RFC 7662 格式 JSON（`active` 必需；active=true 时 `sub`/`exp` 必需）
  - [ ] token 无效时返回 `{"active": false}`（HTTP 200，不是 4xx）
  - [ ] 响应时间 < 500ms（避免触发 agent-flow 超时）

- [ ] **保证 sub 稳定**
  - [ ] 同一用户的每次 introspection 返回相同 `sub`
  - [ ] `sub` 不随 token 续签而变化

- [ ] **提供 agent-flow 调用凭证**
  - [ ] 给 agent-flow 一个 client_id / client_secret（或 bearer token）
  - [ ] 该凭证仅用于 agent-flow 调 introspection，权限受限

- [ ] **前端改造**
  - [ ] 用户登录后，调 agent-flow `/ext/*` 时同时传两个 header
  - [ ] 处理 401 `invalid_user_token`：刷新 token 或重新登录

- [ ] **可选：撤销 webhook**
  - [ ] 用户被撤销时，POST `/api/v1/ext/webhooks/user-revoked`
  - [ ] 请求带 API Key 鉴权（与其它 ext 接口一致）

### C. 常见问题

**Q1: 接入方没有 OAuth2 server，怎么实现 introspection？**

A: 写一个简单的 HTTP 端点即可。伪代码：

```python
@app.post("/oauth/introspect")
async def introspect(token: str = Form(...)):
    # 1. 查 token 是否有效(查 Redis/DB)
    user = await token_store.get(token)
    if not user:
        return {"active": False}
    # 2. 返回标准格式
    return {
        "active": True,
        "sub": user.id,
        "username": user.username,
        "email": user.email,
        "exp": user.token_exp,
        "attrs": {"dept": user.dept, "role": user.role},
    }
```

**Q2: 接入方能否用自己的 token 格式（非 JWT）？**

A: 可以。introspection 的 `token` 字段对格式无要求，接入方完全可以传一个不透明的随机字符串（opaque token），由接入方自己的 introspection 端点去查缓存/DB 校验。这正是 RFC 7662 的典型用法。

**Q3: 用户切换设备后，历史会话还能看到吗？**

A: 只要接入方侧的 `sub` 不变（同一用户），切换设备后 agent-flow 解析出同一个 `end_user_id`，历史会话完全可见。这是 EndUser 体系的核心价值——解决了 visitor_id 不跨设备的痛点。

**Q4: introspection 缓存期间用户被撤销了怎么办？**

A: 最坏情况：缓存 TTL 内（默认 60s）该用户仍能调用。缓解：
- TTL 设短（最低 5s）
- 接入方实现撤销 webhook（§3.4），撤销立即生效
- 高敏操作可让接入方前端在调用前自检 token

**Q5: MCP server 怎么知道一个用户对应什么权限？**

A: MCP server 拿到的就是它本就认识的 token（user_token 模式下透传接入方用户的 token）。它解析 token 拿到用户身份后，按自己已有的鉴权体系做决策：
- 解析 JWT claims 拿到 sub/role/dept → 做 RBAC
- 调自己的 introspection 查权限
- 查本地 ACL

agent-flow 不掺和 MCP server 的权限决策——它把 token 透传过去，剩下都是 MCP server 自己的事。

**Q6: 为什么不直接用 OIDC userinfo 端点（RFC 7662 vs OIDC）？**

A: OIDC userinfo 要求接入方有完整的 OIDC provider 实现（含 discovery、JWKS 等），对没有 SSO 的接入方成本太高。RFC 7662 introspection 更轻量，只要一个 POST 端点即可。如果接入方已有 OIDC provider，也可以直接用其 introspection 端点（标准 OIDC provider 都同时提供）。

**Q7: 多个 API Key 共用一个接入方时，EndUser 会重复吗？**

A: 会。每个 API Key 下有独立的 EndUser 记录（即便 `sub` 相同）。这是有意设计——不同 API Key 代表不同的接入场景（如测试 vs 生产、不同产品线），数据需要隔离。如果业务上需要打通，由接入方自己在多个 API Key 间协调。

---

## 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-07-21 | v0.1 | 初稿 |
| 2026-07-21 | v0.2 | 删除 MCP 调用的 HMAC 签名/自定义 header 机制：MCP server 已有自身鉴权，agent-flow 不重复鉴权 |
| 2026-07-21 | v0.3 | MCP 调用改为 token 透传方案：新增 `McpConnection.auth_mode`（static / user_token），MCP server 零改造，删除全部 `X-Agentflow-*` header，撤销 webhook 改用 API Key 鉴权 |

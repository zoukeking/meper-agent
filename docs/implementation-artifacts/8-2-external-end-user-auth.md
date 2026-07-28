---
baseline_commit: NO_VCS
---

# Story 8.2: 外部终端用户身份认证（P1 核心链路）

**Epic:** Epic 8 — 外部 API 集成
**Status:** done (P1/P2/P3 + 前端 UI 已实施, P4 接入方 SDK 待做)
**Story ID:** 8-2
**Story Key:** 8-2-external-end-user-auth

> **实施记录**（2026-07-21）:
> - P1 核心链路 → commit `4adab13`
> - P2 MCP token 透传 → commit `4cbb1af`
> - 前端 API Key 管理 UI → commit `54061d7`
> - P3 调用日志与 Token 统计 → commit `76459d7`
> - 全量测试 1131 通过, ruff/mypy 干净
> - 设计文档同步到 v0.6 (`docs/planning-artifacts/external-user-auth-design.md`)

## Story

As a 第三方接入方，
I want 让我代表的真实终端用户（而非我的 API Key）在 agent-flow 中有独立的身份与会话，
So that 不同终端用户的会话互相隔离，MCP 调用能透传用户身份做权限决策，平台能按用户审计与统计 token 消耗。

> **关键背景**：
> - 当前外部接入链路用 `{owner_user_id}:{visitor_id}` 复合字符串做会话隔离（`backend/app/api/v1/ext/agents.py:150,186,233,284,323,368`），visitor_id 是前端 localStorage 生成的 UUID，不可信、不跨设备、无属性
> - API Key 体系（`ApiKeyPrincipal`、`auth_and_rate_limit`）已稳定运行
> - MCP server 是自研，与接入方共用同一身份体系，能直接解析接入方 token
> - 详细设计见 [`planning-artifacts/external-user-auth-design.md`](../planning-artifacts/external-user-auth-design.md) §1-4
> - 本 Story 只做 **P1 核心链路**（身份校验 + 会话归属替换）；MCP token 透传（P2）、调用日志与 token 统计（P3）是后续独立 Story
> - 设计原则：最小改动，**ApiKey 仅加一个 `user_info_url` 字段**，McpConnection 零改动，不引入 EndUser 主数据表

## Acceptance Criteria (BDD)

### AC1: ApiKey 数据模型与 Schema 扩展

**Given** 平台需要支持终端用户身份认证
**When** 审查 `backend/app/models/api_key.py`
**Then** `ApiKey` 模型在现有字段基础上新增一个字段：
```python
user_info_url: str = Field(
    default="",
    max_length=500,
    description="接入方 introspection 端点 URL。空=兼容模式(visitor_id);有值=回调验证模式(X-User-Token)",
)
```
**And** 审查 `backend/app/schemas/api_key.py`，以下 Schema 都加上 `user_info_url: str | None = Field(default=None)`：
- `ApiKeyCreate`
- `ApiKeyUpdate`
- `ApiKeyResponse`
- `ApiKeyCreateResponse`
**And** `user_info_url` 字段不需要数据库索引（不参与查询）
**And** 存量 ApiKey 文档（无此字段）读取时默认为空字符串，行为不变

### AC2: ApiKey CRUD 透传新字段

**Given** 已实现的 ApiKey CRUD 接口
**When** 调用 `POST /api/v1/api-keys`（创建）或 `PUT /api/v1/api-keys/{id}`（更新）
**Then** 请求体支持 `user_info_url` 字段（可选）
**And** Service 层 `ApiKeyService.create_api_key` 和 `update_api_key` 的签名新增 `user_info_url` 参数（默认 `""`，向后兼容现有调用方）
**And** 创建时写入 doc（`backend/app/services/api_key_service.py:142-156` 的 doc 构造）
**And** 更新时加入 `set_fields` 逻辑（`api_key_service.py:238-272`），仅在参数非 None 时更新
**And** `GET /api/v1/api-keys` 和 `GET /api/v1/api-keys/{id}` 响应包含 `user_info_url` 字段（`_doc_to_response` `api_keys.py:26-41` 补字段）

### AC3: ApiKeyPrincipal 携带 user_info_url 和解析后的 user_id

**Given** 外部请求进入 `/api/v1/ext/*`
**When** `get_api_key_principal`（`backend/app/core/auth_apikey.py:69-106`）完成 API Key 校验
**Then** `ApiKeyPrincipal` dataclass 新增两个字段：
```python
user_info_url: str = ""        # 来自 ApiKey doc,空=兼容模式
user_id: str | None = None     # 解析后的稳定 user_id(见 AC4/AC5),兼容模式为 None(由路由层拼)
```
**And** 现有 `ApiKeyPrincipal` 的其它字段和方法（`has_scope`/`require_scope`/`can_access_*`）行为不变
**And** 现有调用方（如 `tests/core/test_auth_apikey.py`、ext 测试 fixture）构造 `ApiKeyPrincipal` 时不传新字段也能工作（有默认值）

### AC4: 兼容模式（user_info_url 为空）—— 行为完全不变

**Given** 一个 `user_info_url` 为空的 API Key
**When** 外部请求带 `Authorization: Bearer af_live_xxx` + `visitor_id`（body 或 query）
**Then** 不读取 `X-User-Token`（即使带了也忽略）
**And** `principal.user_id` 保持为 `None`（由路由层用 `{owner}:{visitor_id}` 拼，见 AC7）
**And** 不调用任何 introspection 端点
**And** 全部行为与本次改造前**完全一致**

### AC5: 回调验证模式（user_info_url 有值）—— 强制 X-User-Token

**Given** 一个配置了 `user_info_url` 的 API Key
**When** 外部请求进入 `/api/v1/ext/*`
**Then** 从请求头读取 `X-User-Token: Bearer {token}`（去掉 Bearer 前缀取 token 部分）
**And** 若 `X-User-Token` 缺失或格式非法 → 抛 `UnauthorizedError(code="EXT_USER_TOKEN_MISSING")`，响应 401：
```json
{"error": {"code": "EXT_USER_TOKEN_MISSING", "message": "X-User-Token header is required for this API key."}}
```
**And** 若 token 存在 → 调用 `UserAuthService.introspect(user_info_url, token)`（见 AC6）
**And** 若 introspection 返回 `active: false` → 抛 `UnauthorizedError(code="EXT_USER_TOKEN_INVALID")`，响应 401：
```json
{"error": {"code": "EXT_USER_TOKEN_INVALID", "message": "User token is invalid or expired."}}
```
**And** 若 introspection 返回 `active: true` → `principal.user_id = f"{owner_user_id}:{sub}"`
**And** 回调验证模式下**不读取 visitor_id**（即使 body 里带了也忽略）

### AC6: Introspection 客户端（带 Redis 缓存）

**Given** 回调验证模式需要校验 user_token
**When** 调用 `UserAuthService.introspect(user_info_url, user_token)`
**Then** 实现位置：新建 `backend/app/services/user_auth_service.py`
**And** 按 RFC 7662 子集发起请求：
- 方法 `POST`，`Content-Type: application/x-www-form-urlencoded`
- 请求体：`token={user_token}`（URL-encoded）
- **不带任何调用方鉴权头**（introspection 端点不校验调用方）
- 超时硬编码 3 秒，不重试
**And** 响应解析为 `IntrospectionResult`（`active: bool`, `sub: str`, `exp: int`, `username: str`, `email: str`, `attrs: dict`）
**And** Redis 缓存：
- key 格式：`extuser:introspect:{sha256(user_token).hexdigest()}`
- value：JSON 序列化的 IntrospectionResult
- TTL 硬编码 60 秒
- 命中且未过期 → 直接返回，不回调
**And** 缓存封装模式复刻 `backend/app/core/api_key_cache.py`（`get_redis_client` + try/except 容错 + `json.dumps/loads`）
**And** **原始 token 永不缓存**（只缓存 sha256 hash 作 key + 结果），也不写日志

### AC7: 降级策略（introspection 失败但 Redis 有缓存）

**Given** introspection HTTP 调用抛出 `httpx.TimeoutException` / `httpx.HTTPStatusError`（5xx）/ 网络异常
**When** Redis 中存在该 token 的未过期缓存
**Then** 使用 stale 缓存结果继续处理（视为 active）
**And** 在响应头加 `X-User-Auth-Stale: true`
**And** 异步记录 warning 日志（含 api_key_id + user_info_url + 错误类型，**不含 token**）
**And** 若 Redis 无缓存 → 抛相应错误：
- 超时 → `ServiceError(code="EXT_USER_SERVICE_TIMEOUT", status_code=504)`
- 5xx / 网络异常 → `ServiceError(code="EXT_USER_SERVICE_UNAVAILABLE", status_code=503)`
- 4xx（非 401/403）→ `ServiceError(code="EXT_USER_SERVICE_UNAVAILABLE", status_code=503)`

### AC8: Ext 路由统一替换 user_id 拼接

**Given** ext 路由原本用 `f"{principal.owner_user_id}:{body.visitor_id}"` 拼 user_id
**When** 改造 ext 路由
**Then** 新增公共 helper（建议放 `backend/app/api/v1/ext/__init__.py`）：
```python
def resolve_user_id(principal: ApiKeyPrincipal, visitor_id: str | None) -> str:
    """两种模式统一的 user_id 解析。"""
    if principal.user_id:  # 回调验证模式已解析
        return principal.user_id
    # 兼容模式
    if visitor_id:
        return f"{principal.owner_user_id}:{visitor_id}"
    return principal.owner_user_id
```
**And** 以下 6 处全部改为调用 `resolve_user_id(principal, body.visitor_id)`（或对应 query 参数）：
- `backend/app/api/v1/ext/agents.py:150`（invoke）
- `backend/app/api/v1/ext/agents.py:186`（stream）
- `backend/app/api/v1/ext/agents.py:233`（resume）
- `backend/app/api/v1/ext/agents.py:284`（list sessions）
- `backend/app/api/v1/ext/agents.py:323`（get session detail）
- `backend/app/api/v1/ext/agents.py:368`（delete session）
**And** `backend/app/api/v1/ext/workflows.py:164` 的 `created_by=principal.owner_user_id` 改为 `created_by=resolve_user_id(principal, body.visitor_id)`（如果 ExtWorkflowInvokeRequest 有 visitor_id 字段）或保持 owner_user_id（如果没有，本 Story 暂不处理 workflow 路径的 user 隔离，留待 P3）
**And** `backend/app/api/v1/ext/tasks.py:49-50` 的归属校验 `doc["created_by"] != principal.owner_user_id` 保持不变（因为 workflow 路径未改 created_by）

### AC9: 错误码与全局异常集成

**Given** 新增的异常都需要走全局异常中间件
**When** 抛出 `UnauthorizedError` / `ServiceError`
**Then** 新增以下错误码常量（建议放 `backend/app/core/errors.py` 或就近定义）：
- `EXT_USER_TOKEN_MISSING` (401)
- `EXT_USER_TOKEN_INVALID` (401)
- `EXT_USER_SERVICE_UNAVAILABLE` (503)
- `EXT_USER_SERVICE_TIMEOUT` (504)
**And** 所有错误自动被 `ExceptionMiddleware`（`backend/app/api/middleware/exception_mw.py:13-32`）转统一 envelope 格式，无需额外注册
**And** 错误响应包含 `request_id` 字段

### AC10: 测试覆盖

**Given** 改动涉及认证核心链路
**When** 编写测试
**Then** 在 `backend/tests/core/test_auth_apikey.py` 增加：
- 兼容模式下 `ApiKeyPrincipal.user_id` 为 None
- 回调验证模式下 `ApiKeyPrincipal.user_id` 为 `{owner}:{sub}`
- 缺 X-User-Token 抛 EXT_USER_TOKEN_MISSING
- introspection active=false 抛 EXT_USER_TOKEN_INVALID
**And** 在 `backend/tests/services/test_user_auth_service.py`（新建）覆盖：
- introspection 成功 / active=false / 超时 / 5xx / 4xx 各种场景
- 缓存命中不回调、缓存未命中回调
- stale 缓存降级
**And** 在 `backend/tests/api/test_ext_agents.py` 增加：
- 回调验证模式 invoke / stream / resume 走通
- 回调验证模式下 visitor_id 被忽略
- 兼容模式下 visitor_id 仍生效
- 401 错误响应格式正确
**And** 在 `backend/tests/api/test_api_keys.py` 增加：
- create / update 时传 user_info_url 持久化成功
- response 里包含 user_info_url 字段

## Tasks / Subtasks

### 阶段 1：数据层扩展（AC1, AC2）

- [ ] **T1** 扩展 ApiKey 模型与 Schema（AC1）
  - [ ] T1.1 在 `backend/app/models/api_key.py:37-58` 给 `ApiKey` 加 `user_info_url: str = Field(default="", max_length=500)`
  - [ ] T1.2 在 `backend/app/schemas/api_key.py` 给 `ApiKeyCreate` / `ApiKeyUpdate` / `ApiKeyResponse` / `ApiKeyCreateResponse` 加 `user_info_url: str | None = Field(default=None)`

- [ ] **T2** Service 和 API 透传字段（AC2）
  - [ ] T2.1 `ApiKeyService.create_api_key`（`api_key_service.py:83-90`）签名加 `user_info_url: str = ""`，在 doc 构造（`:142-156`）写入
  - [ ] T2.2 `ApiKeyService.update_api_key`（`:216-223`）签名加 `user_info_url: str | None = None`，在 `set_fields`（`:238-272`）加分支（仅非 None 时更新）
  - [ ] T2.3 `backend/app/api/v1/api_keys.py` 的 create（`:50-75`）和 update（`:120-137`）端点透传 `user_info_url=body.user_info_url`
  - [ ] T2.4 `_doc_to_response`（`:26-41`）补 `user_info_url` 字段

### 阶段 2：Introspection 客户端（AC6, AC7）

- [ ] **T3** 新建 introspection 缓存（AC6 缓存部分）
  - [ ] T3.1 新建 `backend/app/core/introspection_cache.py`，复刻 `api_key_cache.py` 的封装模式
  - [ ] T3.2 实现 `cache_introspection(token_hash, result, ttl=60)` / `get_cached_introspection(token_hash)` / `invalidate(token_hash)`
  - [ ] T3.3 key 格式 `extuser:introspect:{sha256_hex}`，TTL 常量 60 秒

- [ ] **T4** 新建 UserAuthService（AC6, AC7）
  - [ ] T4.1 新建 `backend/app/services/user_auth_service.py`
  - [ ] T4.2 定义 `IntrospectionResult` Pydantic 模型（`active`/`sub`/`exp`/`username`/`email`/`attrs`）
  - [ ] T4.3 实现 `async def introspect(user_info_url: str, user_token: str) -> IntrospectionResult`：
    - 计算 `token_hash = sha256(user_token).hexdigest()`
    - 查缓存，命中且未过期直接返回
    - 未命中用 `httpx.AsyncClient(timeout=3.0)` 发 POST（form-encoded `token=...`，不带鉴权）
    - 解析响应（HTTP 200 + JSON），构造 IntrospectionResult
    - HTTP 200 + `active:false` → 返回 inactive 结果（不缓存，避免撤销后还被缓存命中）
    - 写缓存（仅 active=true 时）
  - [ ] T4.4 异常分支（AC7）：
    - `httpx.TimeoutException` → 有缓存用 stale（响应头标记），无缓存抛 `ServiceError(EXT_USER_SERVICE_TIMEOUT, 504)`
    - `httpx.HTTPStatusError` 5xx 或 `httpx.HTTPError` → 有缓存用 stale，无缓存抛 `ServiceError(EXT_USER_SERVICE_UNAVAILABLE, 503)`
    - 4xx 非 401/403 → 同上
  - [ ] T4.5 stale 标记通过 `contextvars.ContextVar` 传递（中间件读后写响应头），或直接由 service 抛特殊返回值让上层处理（实现时选简单的）

### 阶段 3：认证链路改造（AC3, AC4, AC5, AC9）

- [ ] **T5** 扩展 ApiKeyPrincipal 和认证入口（AC3, AC5）
  - [ ] T5.1 `backend/app/core/auth_apikey.py` 的 `ApiKeyPrincipal` dataclass 加 `user_info_url: str = ""` 和 `user_id: str | None = None`
  - [ ] T5.2 `get_api_key_principal`（`:69-106`）签名改为接收 Request（FastAPI 自动注入），改为：
    - 读 `Authorization` 校验 API Key（现有逻辑，产出 doc）
    - 把 `doc.get("user_info_url", "")` 赋给 principal
    - 若 `user_info_url` 非空：
      - 从 `request.headers.get("X-User-Token")` 提取 token（去 Bearer 前缀）
      - 缺失/格式错 → 抛 `UnauthorizedError(EXT_USER_TOKEN_MISSING)`
      - 调 `UserAuthService.introspect(user_info_url, token)`
      - `active: false` → 抛 `UnauthorizedError(EXT_USER_TOKEN_INVALID)`
      - `active: true` → `principal.user_id = f"{doc['owner_user_id']}:{result.sub}"`
    - 若 `user_info_url` 为空：`principal.user_id = None`（兼容模式）
  - [ ] T5.3 在 `backend/app/core/errors.py` 或就近定义新错误码常量（AC9）

### 阶段 4：Ext 路由改造（AC8）

- [ ] **T6** 统一 user_id 解析 helper（AC8）
  - [ ] T6.1 在 `backend/app/api/v1/ext/__init__.py` 加 `resolve_user_id(principal, visitor_id)` 函数

- [ ] **T7** 替换 ext/agents.py 的 6 处拼接（AC8）
  - [ ] T7.1 `:150` invoke → `resolve_user_id(principal, body.visitor_id)`
  - [ ] T7.2 `:186` stream → 同上
  - [ ] T7.3 `:233` resume → 同上
  - [ ] T7.4 `:284` list sessions → `resolve_user_id(principal, visitor_id)`（visitor_id 是 query 参数）
  - [ ] T7.5 `:323` get session detail → 同上
  - [ ] T7.6 `:368` delete session → 同上

- [ ] **T8** workflow 路径评估（AC8）
  - [ ] T8.1 检查 `ExtWorkflowInvokeRequest` 是否有 visitor_id 字段（`schemas/ext_api.py`）
  - [ ] T8.2 若有 → `workflows.py:164` 改为 `resolve_user_id(principal, body.visitor_id)`
  - [ ] T8.3 若无 → 保持 `principal.owner_user_id`，记 TODO 注释（P3 处理 workflow 的 user 隔离）

### 阶段 5：stale 响应头注入（AC7）

- [ ] **T9** 中间件支持 stale 标记（AC7）
  - [ ] T9.1 用 ContextVar（`_introspect_stale: ContextVar[bool]`）在 `UserAuthService` 降级时置 True
  - [ ] T9.2 在 `ExtApiStatsMiddleware`（`ext/__init__.py:59-85`）或新增轻量中间件里读 ContextVar，写响应头 `X-User-Auth-Stale: true`

### 阶段 6：测试（AC10）

- [ ] **T10** 单元测试
  - [ ] T10.1 `tests/core/test_auth_apikey.py` 加 case：兼容模式 user_id=None、回调模式 user_id 解析、缺 token 抛错、active=false 抛错
  - [ ] T10.2 新建 `tests/services/test_user_auth_service.py`：
    - 成功路径（mock httpx 返回 active:true）
    - active:false（不缓存）
    - 超时 / 5xx / 4xx 各种异常
    - 缓存命中不回调
    - stale 降级（缓存有 + httpx 抛异常）
  - [ ] T10.3 `tests/api/test_ext_agents.py` 加 case：回调模式 invoke/stream/resume、visitor_id 被忽略、兼容模式 visitor_id 仍生效、401 响应格式

- [ ] **T11** API 测试
  - [ ] T11.1 `tests/api/test_api_keys.py` 加 case：create/update 传 user_info_url 持久化、response 包含字段

- [ ] **T12** 回归测试
  - [ ] T12.1 跑全量 ext 测试套件确认兼容模式行为不变
  - [ ] T12.2 跑全量 api_keys 管理端测试确认 CRUD 正常

## Out of Scope（明确不做）

以下留给后续 Story：

- **MCP 调用透传 user_token（P2）**：`mcp/loader.py` 改造、ContextVar 注入 user_token、按 token 是否存在二分取凭证
- **调用日志与 Token 统计（P3）**：`ext_api_call_logs` collection、两阶段写入、stats 端点扩展
- **撤销 webhook**：TTL 60s 窗口期由后续按需补充
- **per-user 限流**：当前只按 api_key_id 限流
- **前端 ApiKey 编辑界面加 user_info_url 输入框**：前端改造独立排期
- **workflow 路径的 user 隔离**：本 Story 只改 agent 路径（详见 T8）

## Risks & Mitigations

| 风险 | 缓解 |
|------|------|
| 存量 ApiKey 缺字段读取异常 | `user_info_url` 默认 `""`，Pydantic 自动容错；增加回归测试 |
| introspection 服务故障导致外部调用全挂 | stale 缓存降级 + 503 明确错误码；接入方可随时清空 `user_info_url` 回退 |
| ext 测试 fixture 全局修改导致回归 | 新字段都有默认值，现有 fixture 构造不传也能跑 |
| `get_api_key_principal` 改签名引入新依赖（Request）影响现有调用 | FastAPI 自动注入 Request，对 Depends 链无感；测试 override 不受影响 |
| introspection 缓存 key 含 token hash 可能泄漏 | sha256 单向不可逆，且仅存 Redis 内存；日志严格禁止打印 token |

## Testing Strategy

- **单元测试**：覆盖 UserAuthService 所有分支、ApiKeyPrincipal 新字段、resolve_user_id helper
- **API 测试**：覆盖 create/update API Key 透传字段、ext 路由两种模式
- **回归测试**：全量 ext + api_keys 测试套件必须全绿
- **手工验证**：
  1. 用旧 API Key（无 user_info_url）调用 ext → 行为完全不变
  2. 创建新 API Key 配 user_info_url → 用 mock introspection 端点验证完整链路
  3. 故意停掉 introspection 端点 → 验证 stale 降级 + 503 错误码
- **不写集成测试**：introspection 调外部 HTTP，单测用 `httpx.MockTransport` 或 `respx` mock

## Implementation Notes

### 关键代码片段参考

**`get_api_key_principal` 改造骨架**（`backend/app/core/auth_apikey.py`）：

```python
async def get_api_key_principal(
    request: Request,
    authorization: str = Header(..., description="Bearer af_live_xxx"),
) -> ApiKeyPrincipal:
    # 1. 解析 + 校验 API Key (现有逻辑,产出 doc)
    full_key = _extract_bearer(authorization)  # 现有
    doc = await ApiKeyService.verify_key(full_key)  # 现有
    if not doc:
        raise UnauthorizedError(code="APIKEY_INVALID", ...)

    # 2. 构造 principal(带 user_info_url)
    principal = ApiKeyPrincipal(
        key_id=doc["_id"],
        owner_user_id=doc["owner_user_id"],
        scopes=doc.get("scopes", []),
        bindings=doc.get("bindings", {}),
        rate_limit=doc.get("rate_limit", 60),
        user_info_url=doc.get("user_info_url", ""),
    )

    # 3. 回调验证模式:解析 user_token + introspection
    if principal.user_info_url:
        user_token = _extract_user_token(request.headers.get("X-User-Token"))
        if not user_token:
            raise UnauthorizedError(code="EXT_USER_TOKEN_MISSING", ...)
        result = await user_auth_service.introspect(principal.user_info_url, user_token)
        if not result.active:
            raise UnauthorizedError(code="EXT_USER_TOKEN_INVALID", ...)
        principal.user_id = f"{principal.owner_user_id}:{result.sub}"

    return principal


def _extract_user_token(header_value: str | None) -> str | None:
    if not header_value:
        return None
    if header_value.startswith("Bearer "):
        return header_value[7:].strip()
    return header_value.strip() or None
```

**`UserAuthService.introspect` 骨架**（`backend/app/services/user_auth_service.py`）：

```python
import hashlib, httpx
from app.core.introspection_cache import cache_introspection, get_cached_introspection

class UserAuthService:
    TIMEOUT = 3.0
    CACHE_TTL = 60

    async def introspect(self, user_info_url: str, user_token: str) -> IntrospectionResult:
        token_hash = hashlib.sha256(user_token.encode()).hexdigest()

        # 1. 查缓存
        cached = await get_cached_introspection(token_hash)
        if cached:
            return cached

        # 2. 回调
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(
                    user_info_url,
                    data={"token": user_token},  # form-encoded
                )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            # 降级 stale (见 AC7)
            stale = await get_cached_introspection(token_hash, include_expired=True)
            if stale:
                _mark_stale()  # ContextVar 置 True
                logger.warning("introspection fallback to stale cache, api_key_url=%s err=%s", user_info_url, type(e).__name__)
                return stale
            if isinstance(e, httpx.TimeoutException):
                raise ServiceError(code="EXT_USER_SERVICE_TIMEOUT", status_code=504, message="User service timeout.")
            raise ServiceError(code="EXT_USER_SERVICE_UNAVAILABLE", status_code=503, message="User service unavailable.")

        # 3. 解析响应
        result = IntrospectionResult(**payload)
        if result.active:
            await cache_introspection(token_hash, result, ttl=self.CACHE_TTL)
        return result
```

### Redis key 命名约定

遵循现有 `{namespace}:{purpose}:{id}` 风格：

| key | 用途 | TTL |
|-----|------|-----|
| `extuser:introspect:{sha256_hex}` | introspection 结果缓存 | 60s |

### 不需要改动的位置

- `backend/app/db/indexes.py`（user_info_url 不需要索引）
- `backend/app/main.py`（无新中间件注册，stale 标记复用 ExtApiStatsMiddleware 或在现有中间件链内消化）
- `backend/app/api/middleware/exception_mw.py`（AppError 子类自动处理）
- `McpConnection` 模型（P2 才改）
- `agent_execution_service.py`（P3 才改）

## References

- 设计文档：[`planning-artifacts/external-user-auth-design.md`](../planning-artifacts/external-user-auth-design.md) §1-4
- 外部 API 基础设计：[`planning-artifacts/external-api-design.md`](./external-api-design.md)
- API Key 现有实现：`backend/app/models/api_key.py`、`backend/app/core/auth_apikey.py`、`backend/app/services/api_key_service.py`
- Ext 路由现有实现：`backend/app/api/v1/ext/__init__.py`、`backend/app/api/v1/ext/agents.py`
- 缓存封装范式：`backend/app/core/api_key_cache.py`
- HTTP 客户端范式：`backend/app/services/webhook_delivery.py`
- RFC 7662 OAuth2 Token Introspection：https://datatracker.ietf.org/doc/html/rfc7662

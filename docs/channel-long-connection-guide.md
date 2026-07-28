# 频道长连接对接指南(免公网)

本指南介绍如何在不暴露公网 IP/域名的情况下,让 IM 平台(飞书、钉钉)的机器人
和 Agent Flow 对话。服务部署在内网、本地开发机、无公网 IP 的服务器上都适用。

## 适用场景

- 本地开发,不想用 ngrok / frp 做公网穿透
- 服务部署在公司内网,防火墙只允许出站访问
- 不想为机器人对接单独暴露公网回调端点

## 原理

传统 webhook 模式:IM 平台需要把消息 POST 到你的公网 URL,所以你必须暴露公网。

长连接模式:你的服务**主动**向 IM 平台发起 WebSocket 连接,平台把消息通过这条
连接推送给你。连接是出站的,所以你的服务不需要任何公网入口,只要能访问外网即可。

```
┌─────────────────┐       WebSocket (出站)        ┌──────────────────┐
│  你的服务        │ ───────────────────────────►  │  IM 平台          │
│ (内网/本地)      │ ◄───────────────────────────  │ (飞书/钉钉)       │
│                 │       事件推送 (入站方向)       │                  │
└─────────────────┘                               └──────────────────┘
```

## 平台支持情况

| 平台 | 长连接支持 | SDK | 状态 |
|---|---|---|---|
| 飞书 (Lark) | ✅ | `lark-oapi` (`ws.Client`) | 已实现 |
| 钉钉 (DingTalk) | ✅ | `dingtalk-stream` (Stream 模式) | 已实现 |
| 企业微信 (WeCom) | ✅ 协议支持 | 无官方 Python SDK | 暂未实现(仍走 webhook) |
| Mock | — | — | 测试用,仅 webhook |

## 配置

### 全局开关(`backend/.env` 或环境变量)

```bash
# 各平台长连接总开关(默认 True,企微默认 False 因首期未实现)
CHANNEL_LARK_LONG_CONNECTION_ENABLED=true
CHANNEL_DINGTALK_LONG_CONNECTION_ENABLED=true
CHANNEL_WECOM_LONG_CONNECTION_ENABLED=false

# 断线重连间隔(秒)
CHANNEL_CONNECTION_RECONNECT_INTERVAL=10
```

设为 `false` 时,该平台的前端创建表单不会显示"长连接"选项,只能用 webhook。

### 创建 channel 时选择模式

后台 → 渠道 → 新建 Channel:
1. 选平台(飞书 / 钉钉)
2. **接收模式** 选 **长连接(免公网)**
3. 填平台凭据(见下方各平台章节)
4. 绑定 Agent → 创建

前端列表会显示一个圆点指示连接状态(绿色=已连接,灰色=已断开)。

---

## 飞书(Lark)长连接对接

### 第 1 步:飞书开放平台建应用

1. https://open.feishu.cn/app → 创建企业自建应用
2. **凭证与基础信息** 记下:
   - **App ID**(`cli_xxxxxxxxxxxxxxxx`)
   - **App Secret**

### 第 2 步:开通权限

**权限管理 → 开通权限**,搜并开通:
- `im:message` — 读取消息
- `im:message:send_as_bot` — 机器人发消息
- `im:chat` — 获取群信息

### 第 3 步:事件订阅选长连接

**事件与回调 → 事件配置**:
1. 订阅方式选 **使用长连接接收事件(推荐)** ← 关键!不要选 HTTP 回调
2. 添加事件:**接收消息 v2.0**(`im.message.receive_v1`)

> 长连接模式不需要填请求地址,不需要验证 challenge。

### 第 4 步:启用机器人 + 发布

- **应用能力 → 机器人**:启用,填机器人名和头像
- **版本管理与发布**:申请发布,管理员审核通过

### 第 5 步:后台配置 channel

1. 后台 → 渠道 → 新建 Channel
2. 平台选 **飞书**
3. **接收模式** 选 **长连接(免公网)**
4. 凭据:
   - App ID = `cli_xxx`
   - App Secret = 飞书给的 secret
   - Verification Token / Encrypt Key:**长连接模式不需要**(留空)
5. 绑定 Agent → 创建

创建后,后端 FastAPI 进程会自动启动一个 WebSocket 连接到飞书。列表里看到绿色圆点
"已连接"即表示连接成功。

### 第 6 步:测试

在飞书里搜你的机器人,单聊发一条 `你好`。后端日志会看到:

```
INFO  connection_started channel=ch_xxx provider=lark
INFO  lark connection established
INFO  POST /api/v1/channels/inbound/... (内部 dispatch)
INFO  agent invoked, reply sent
```

飞书里收到 agent 的回复。

### 飞书排错

| 现象 | 排查 |
|---|---|
| 创建 channel 后状态一直是灰色"未启动" | 后端没启动,或 `CHANNEL_LARK_LONG_CONNECTION_ENABLED=false` |
| 状态变绿但飞书发消息没反应 | ① 事件订阅没选长连接模式 ② 没加 `im.message.receive_v1` 事件 ③ 应用没发布 |
| 日志报 `InvalidCredentialsError` | App ID / App Secret 填错 |
| 日志报 `connection_failed ... reconnecting` | 网络问题(出站到 `open.feishu.cn` 不通)或凭据失效 |

---

## 钉钉(DingTalk)长连接对接

### 第 1 步:钉钉开发者后台建应用

1. https://open-dev.dingtalk.com → 应用开发 → 创建应用(企业内部应用)
2. **凭证与基础信息** 记下:
   - **AppKey**(`dingxxxxx`)
   - **AppSecret**

### 第 2 步:开通权限 + 事件订阅

**权限管理**:开通机器人相关权限(收发消息)。

**开发配置 → 事件订阅**:
1. 推送方式选 **Stream 模式** ← 关键!不要选 Webhook
2. 订阅机器人消息事件

### 第 3 步:启用机器人

**应用能力 → 机器人**:启用,配置机器人。

**版本管理与发布**:发布应用,管理员审核。

### 第 4 步:后台配置 channel

1. 后台 → 渠道 → 新建 Channel
2. 平台选 **钉钉**
3. **接收模式** 选 **长连接(免公网)**
4. 凭据:
   - App Key = `dingxxx`
   - App Secret = 钉钉给的 secret
   - Webhook URL:**长连接模式不需要**(留空)
5. 绑定 Agent → 创建

### 第 5 步:测试

在钉钉里 @机器人 发消息,后端日志显示连接 + agent 调用 + 回复发送。钉钉里收到回复。

> **钉钉发消息机制**:钉钉机器人回复用入站消息里的 `session_webhook`(临时回复 URL,
> 约 2 小时过期)。系统自动从入站消息里提取并透传,无需额外配置。如果你想做主动推送
> (非回复),才需要额外配置固定的 `webhook_url`。

---

## 部署架构

### 单实例部署(默认)

长连接管理器(`ChannelConnectionManager`)是 FastAPI 进程内的单例。每个
`receive_mode=long_connection` 且 `enabled=true` 的 channel 会被启动一个独立的
asyncio task 维护连接。

```
FastAPI 进程
├── 业务 HTTP API
├── ChannelConnectionManager (单例, lifespan 启动)
│   ├── lark channel A → ws.Client task (线程)
│   ├── lark channel B → ws.Client task (线程)
│   └── dingtalk channel C → stream client task
└── 其他后台 task (TriggerScheduler, EventBridge...)

Celery worker 进程
└── 不跑长连接 (避免重复建连)
```

### 多实例部署的注意(首期不支持)

如果一个 channel 在多个 FastAPI 实例上同时启用,会建立多条重复连接,导致同一条
消息被处理多次。首期建议单实例部署。多实例需要分布式锁协调,留作后续。

### 配置变更热加载

后台 CRUD(create/update/delete/enable/disable)会自动通知 manager 增删连接,
**不需要重启后端**。改凭据后 1 秒内新连接生效。

### 断线重连

- **飞书**:SDK 内置 `auto_reconnect=True`,自动重连
- **钉钉**:SDK 内置重连
- **管理器层**:连接意外退出后,`_run_channel` 循环按 `CHANNEL_CONNECTION_RECONNECT_INTERVAL`
  (默认 10 秒)重新建连

---

## 切换模式

### webhook → 长连接

后台编辑 channel,接收模式改成"长连接",保存。manager 自动:
1. 停掉旧 webhook 配置(其实 webhook 入口还在,只是不再被平台调用)
2. 启动新的长连接客户端

记得**同时在 IM 平台后台**把事件订阅方式也改成"长连接 / Stream 模式"。

### 长连接 → webhook

反向操作:后台改成 webhook + 在 IM 平台改回 HTTP 回调 + 填回调 URL(带 `?secret=`)。

---

## 监控要点

- **连接状态**:前端列表的绿/灰圆点,或 `GET /api/v1/channels/{id}` 的
  `connection_status` 字段(`long_connection_connected` /
  `long_connection_disconnected` / `not_long_connection`)
- **后端日志**:搜索 `connection_started` / `connection_failed` / `connection_stopped`
- **事件处理**:与 webhook 模式共享 `inbound_event_logs` 集合,可统一监控

---

## FAQ

**Q: 长连接模式需要 MongoDB / Redis 吗?**
A: 需要。和 webhook 模式一样,事件走 `ChannelService.create_or_dedup_event` 落库
+ Celery 异步处理,只是事件来源不同。

**Q: 一个 channel 能同时用两种模式吗?**
A: 不能。`receive_mode` 是单选,一个 channel 要么 webhook 要么长连接。如果想双保险,
建两个 channel 绑同一个 agent,一个 webhook 一个长连接(IM 平台只能配一种回调方式,
所以实际上也做不到双通道)。

**Q: 长连接模式还能用 webhook_secret 吗?**
A: webhook_secret 是 webhook 模式的二级校验,长连接模式不需要(连接本身就是认证的)。
字段仍存在但长连接模式忽略它。

**Q: 企微什么时候支持长连接?**
A: 企微有长连接协议(官方支持),但没有成熟的 Python SDK。首期未实现。如果需要,
可以基于 `websockets` 库手写客户端,工作量约 1-2 天。可在后续迭代补上。

**Q: 后端重启时,长连接会断吗?**
A: 会。重启时连接断开,Celery 正在处理的消息会继续处理完(持久化在 event log)。
重启后 manager 自动重连。重启窗口期内到达的消息会因连接断开而丢失(平台不会
缓存太久)。生产环境建议用进程管理器(systemd / docker restart:always)快速恢复。

**Q: 如何彻底禁用长连接功能?**
A: 把三个 `CHANNEL_*_LONG_CONNECTION_ENABLED` 都设为 `false`。manager 仍会启动
但不会注册任何 factory,已存在的长连接 channel 会显示"未启动",不影响 webhook 模式。

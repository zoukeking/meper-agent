# Agent Flow 独立客户端

独立于 `frontend-studio/` 管理中后台的终端用户对话客户端。它只包含登录、Agent 选择、本人会话管理和完整对话体验。

## 本地运行

```bash
npm install
npm run dev
```

默认地址为 `http://127.0.0.1:3001`，开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`。

## 环境变量

- `VITE_API_BASE`: API 基础路径，开发环境默认使用 `/api`。
- `VITE_STREAM_BASE`: SSE 后端 origin。未设置时回退到 `VITE_API_BASE`。

## 已支持的后端能力

- JWT 登录、刷新和退出
- 已发布 Agent 选择
- 会话新建、切换、删除和历史回放
- text、thinking、tool_call、tool_result、interrupt SSE 事件
- 知识库自动召回、通用工具参数和结果
- 图片与文档上传、会话文件预览和下载
- Markdown、GFM 表格、代码、ECharts 与 Mermaid
- 对话内写操作批准和拒绝
- 桌面侧栏、移动抽屉、动态视口和安全区适配

import { useEffect } from 'react'

import { applyEmbedConfig, setUserToken } from '../api/client'

const CONFIG_TYPE = 'agentflow:config'
const TOKEN_TYPE = 'agentflow:user_token'

function getAllowedOrigins(): string[] {
  const raw = import.meta.env.VITE_ALLOWED_PARENT_ORIGINS ?? ''
  return raw
    .split(',')
    .map((s: string) => s.trim())
    .filter(Boolean)
}

function isInIframe(): boolean {
  try {
    return window.self !== window.top
  } catch {
    // 跨域访问 window.top 抛 SecurityError → 必然在 iframe 内
    return true
  }
}

/**
 * iframe 嵌入时接收宿主页（chat-widget.js）注入的配置：
 *   agentflow:config     { apiKey, userToken? } → applyEmbedConfig（data-api-key + cookie token 模式）
 *   agentflow:user_token { token }              → setUserToken（带白名单的强隔离通道，可选）
 * 仅 iframe 内生效。bootstrapAuth 会主动发 agentflow:request_config 请求配置。
 *
 * 安全：
 * - 两类消息都只接受来自直接父页（event.source === window.parent），防其他 iframe 注入。
 * - config 通道（apiKey + userToken）由客户部署的 chat-widget.js 发出：apiKey 公开写在客户 HTML；
 *   userToken 由该脚本从宿主页 cookie（默认 mep-access-token）读取，属同一信任链，source 校验即可。
 *   多客户通用 client 模式下客户域名不定，无法穷举白名单；真正的身份校验在后端 RFC 7662
 *   introspection（无效 token 必拒，伪造 token 无法冒充）。
 * - user_token 通道是终端用户身份凭据（敏感），额外校验 VITE_ALLOWED_PARENT_ORIGINS 白名单，
 *   白名单未配置时拒绝（fail-safe）；留给需要更强隔离的单客户定制场景。
 */
export function useParentToken(): void {
  useEffect(() => {
    if (!isInIframe()) return

    const allowed = getAllowedOrigins()

    const onMessage = (event: MessageEvent) => {
      // 只信任直接父页（嵌入方），忽略来自其他 iframe/window 的消息
      if (event.source !== window.parent) return
      const data = event.data as
        | { type?: string; apiKey?: string; userToken?: string; token?: string }
        | null
      if (!data) return

      if (data.type === CONFIG_TYPE && typeof data.apiKey === 'string' && data.apiKey) {
        applyEmbedConfig(data.apiKey, data.userToken)
      } else if (data.type === TOKEN_TYPE && typeof data.token === 'string') {
        // 终端 token 敏感：白名单未配置或非白名单 origin → 拒绝
        if (allowed.length === 0 || !allowed.includes(event.origin)) return
        setUserToken(data.token)
      }
    }

    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
}

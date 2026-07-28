import { REFRESH_TOKEN_KEY, useAuthStore } from '../store/auth'
import type { TokenResponse } from '../types'

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'
const BUILD_API_KEY = import.meta.env.VITE_PUBLIC_API_KEY ?? ''
const VISITOR_ID_KEY = 'meper_client_visitor_id'

/** 运行时注入的 API Key（嵌入模式由父页 chat-widget.js 经 postMessage 传入，
 * 优先于 build 内置 BUILD_API_KEY）。仅存内存，不落 localStorage。 */
let embedApiKey: string | null = null
export function setEmbedApiKey(key: string | null): void {
  embedApiKey = key
}
function effectiveApiKey(): string {
  return embedApiKey ?? BUILD_API_KEY
}

/** 鉴权模式：build 有 key 或运行时注入 key → apikey；否则 jwt。
 * ES module live binding：setAuthMode 后，所有 import 处运行时取最新值。 */
export let AUTH_MODE: 'jwt' | 'apikey' = BUILD_API_KEY ? 'apikey' : 'jwt'
export function setAuthMode(mode: 'jwt' | 'apikey'): void {
  AUTH_MODE = mode
}

// Callback 模式的终端用户 token（X-User-Token），由宿主页通过 postMessage
// 注入。仅存内存，不落 localStorage。
let userToken: string | null = null
export function setUserToken(token: string | null): void {
  userToken = token
}
export function getUserToken(): string | null {
  return userToken
}

let refreshPromise: Promise<string | null> | null = null

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

/** 生成 UUID（兼容非安全上下文，与 use-chat.genId 同策略）。 */
function genId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

export function getVisitorId(): string {
  let id = localStorage.getItem(VISITOR_ID_KEY)
  if (!id) {
    id = genId()
    localStorage.setItem(VISITOR_ID_KEY, id)
  }
  return id
}

async function readError(response: Response): Promise<ApiError> {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  const data = payload as
    | {
      detail?: unknown
      message?: string
      code?: string
      error?: { message?: string; code?: string }
    }
    | null
  const detail = data?.detail
  const nestedDetail =
    detail && typeof detail === 'object'
      ? (detail as { message?: unknown; code?: unknown })
      : null
  const message =
    typeof detail === 'string'
      ? detail
      : typeof nestedDetail?.message === 'string'
        ? nestedDetail.message
      : typeof data?.error?.message === 'string'
        ? data.error.message
      : typeof data?.message === 'string'
        ? data.message
        : `请求失败（${response.status}）`
  const code =
    data?.code ??
    data?.error?.code ??
    (typeof nestedDetail?.code === 'string' ? nestedDetail.code : undefined)
  return new ApiError(message, response.status, code)
}

async function refreshAccessToken(): Promise<string | null> {
  if (refreshPromise) return refreshPromise
  refreshPromise = (async () => {
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
    if (!refreshToken) return null
    const response = await fetch(apiUrl('/v1/auth/refresh'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
    if (!response.ok) {
      useAuthStore.getState().clear()
      return null
    }
    const bundle = (await response.json()) as TokenResponse
    useAuthStore.getState().setSession(bundle)
    return bundle.access_token
  })().finally(() => {
    refreshPromise = null
  })
  return refreshPromise
}

export async function ensureAccessToken(): Promise<string | null> {
  if (AUTH_MODE === 'apikey') return null
  return useAuthStore.getState().accessToken ?? refreshAccessToken()
}

/** apikey 模式：附加 API Key +（若有）用户 token（X-User-Token header）。
 * 访客 ID（visitor_id）由调用方按后端约定放进 query 或 body。
 * 后端按 API Key 的 user_info_url 自动选用 legacy(visitor_id) / callback(user_token)。 */
export function applyApiKeyHeaders(headers: Headers): void {
  headers.set('Authorization', `Bearer ${effectiveApiKey()}`)
  const token = getUserToken()
  if (token) headers.set('X-User-Token', token)
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (AUTH_MODE === 'apikey') {
    applyApiKeyHeaders(headers)
  } else {
    const token = useAuthStore.getState().accessToken
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }
  const response = await fetch(apiUrl(path), { ...init, headers })
  if (response.status === 401 && retry && AUTH_MODE === 'jwt') {
    const nextToken = await refreshAccessToken()
    if (nextToken) return apiRequest<T>(path, init, false)
  }
  if (!response.ok) throw await readError(response)
  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) return response.json() as Promise<T>
  return response.blob() as Promise<T>
}

/** 是否在 iframe 内（跨域访问 window.top 抛 SecurityError → 必然在 iframe 内）。 */
export function inIframe(): boolean {
  try {
    return window.self !== window.top
  } catch {
    return true
  }
}

// 嵌入 handshake：iframe 内 bootstrapAuth 等 chat-widget.js 注入 API Key（data-api-key）。
let resolveEmbedReady: (() => void) | null = null

/** 父页（chat-widget.js）经 postMessage 注入嵌入配置时调用。
 * apiKey 必填；userToken 可选（callback 模式）。 */
export function applyEmbedConfig(apiKey: string, userToken?: string | null): void {
  if (apiKey) setEmbedApiKey(apiKey)
  setAuthMode('apikey')
  if (userToken) setUserToken(userToken)
  if (resolveEmbedReady) {
    const resolve = resolveEmbedReady
    resolveEmbedReady = null
    resolve()
  }
}

export async function bootstrapAuth(): Promise<void> {
  // iframe 嵌入：等父页注入 API Key（data-api-key），优先于 build 内置。
  if (inIframe()) {
    await new Promise<void>((resolve) => {
      resolveEmbedReady = resolve
      window.parent.postMessage({ type: 'agentflow:request_config' }, '*')
      // 超时降级：未收到则按 build key / jwt 继续
      window.setTimeout(() => {
        if (resolveEmbedReady) {
          resolveEmbedReady = null
          resolve()
        }
      }, 4000)
    })
  }
  if (AUTH_MODE === 'apikey' || effectiveApiKey()) {
    if (AUTH_MODE !== 'apikey') setAuthMode('apikey')
    useAuthStore.getState().setInitialized(true)
    return
  }
  try {
    await refreshAccessToken()
  } finally {
    useAuthStore.getState().setInitialized(true)
  }
}

export function streamUrl(path: string): string {
  // 用 || 而非 ??：VITE_STREAM_BASE 为空字符串时也要回退到 API_BASE（?? 不对空串 fallback）
  const base = import.meta.env.VITE_STREAM_BASE || API_BASE
  return `${base}${path}`
}

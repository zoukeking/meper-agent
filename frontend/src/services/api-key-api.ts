/**
 * API Key management service — wraps backend /api-keys endpoints.
 *
 * Uses the shared apiClient instance (auto auth header + 401 refresh).
 * Response fields are snake_case per backend contract.
 */
import { apiClient } from './api-client'

/* ─── Types (snake_case, matches backend schemas) ─── */

export type ApiKeyStatus = 'active' | 'revoked'

export interface ApiKeyBindings {
  agents: string[]
  workflows: string[]
}

export interface ApiKey {
  id: string
  name: string
  key_prefix: string
  owner_user_id: string
  scopes: string[]
  bindings: ApiKeyBindings
  rate_limit: number
  status: ApiKeyStatus
  expires_at: string | null
  last_used_at: string | null
  /** 接入方 introspection 端点 URL。空=兼容模式(visitor_id);有值=回调验证模式(强制 X-User-Token)。 */
  user_info_url: string
  created_at: string
  updated_at: string
}

/** Returned only once at creation time — includes the raw key. */
export interface ApiKeyCreated extends Omit<ApiKey, 'updated_at'> {
  key: string
}

export interface ApiKeyCreateInput {
  name: string
  scopes: string[]
  bindings?: ApiKeyBindings
  rate_limit?: number
  expires_at?: string | null
  user_info_url?: string | null
}

export interface ApiKeyUpdateInput {
  name?: string
  scopes?: string[]
  bindings?: ApiKeyBindings
  rate_limit?: number
  expires_at?: string | null
  user_info_url?: string | null
}

export interface ApiKeyListParams {
  page?: number
  page_size?: number
}

export interface ApiKeyListResponse {
  items: ApiKey[]
  total: number
  page: number
  page_size: number
}

/* ─── All available scopes ─── */

export const ALL_API_KEY_SCOPES = [
  'agents:read',
  'agents:invoke',
  'workflows:read',
  'workflows:invoke',
  'executions:read',
] as const

export const SCOPE_LABELS: Record<string, string> = {
  'agents:read': 'Agent 查看',
  'agents:invoke': 'Agent 调用',
  'workflows:read': '工作流 查看',
  'workflows:invoke': '工作流 调用',
  'executions:read': '执行 查看',
}

/* ─── API ─── */

export const apiKeyApi = {
  async list(params: ApiKeyListParams = {}): Promise<ApiKeyListResponse> {
    const res = await apiClient.get<ApiKeyListResponse>('/api/v1/api-keys', {
      params: {
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
      },
    })
    return res.data
  },

  async get(id: string): Promise<ApiKey> {
    const res = await apiClient.get<ApiKey>(`/api/v1/api-keys/${id}`)
    return res.data
  },

  async create(input: ApiKeyCreateInput): Promise<ApiKeyCreated> {
    const res = await apiClient.post<ApiKeyCreated>('/api/v1/api-keys', input)
    return res.data
  },

  async update(id: string, input: ApiKeyUpdateInput): Promise<ApiKey> {
    const res = await apiClient.put<ApiKey>(`/api/v1/api-keys/${id}`, input)
    return res.data
  },

  async revoke(id: string): Promise<void> {
    await apiClient.delete(`/api/v1/api-keys/${id}`)
  },
}

/* ─── Query key factory ─── */

export const apiKeyKeys = {
  all: ['api-keys'] as const,
  lists: () => [...apiKeyKeys.all, 'list'] as const,
  list: (params: ApiKeyListParams) => [...apiKeyKeys.lists(), params] as const,
  details: () => [...apiKeyKeys.all, 'detail'] as const,
  detail: (id: string) => [...apiKeyKeys.details(), id] as const,
}

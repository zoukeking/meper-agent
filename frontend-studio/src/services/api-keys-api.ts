/**
 * API Keys service — wraps backend /api-keys endpoints (对外接入 Key).
 *
 * An API Key is the credential third parties use to call the /ext surface
 * (embedded chat / iframe). It carries: scopes (permissions), resource
 * bindings (which agents/workflows it may touch), a per-minute rate limit,
 * and `user_info_url` — the Story 8.2 mode switch:
 *   - empty  → legacy mode (end-users identified by visitor_id, anonymous)
 *   - set    → callback mode (RFC 7662 introspection of X-User-Token → sub)
 *
 * The raw key is returned exactly once on create (ApiKeyCreateResponse.key);
 * afterwards only `key_prefix` is ever exposed.
 *
 * Uses the shared apiClient (auto auth header + 401 refresh). Response fields
 * are snake_case per backend contract (see app/schemas/api_key.py).
 */
import { apiClient } from '../lib/api-client'

/* ─── Enums / constants (mirror backend ApiKeyScope / ApiKeyStatus) ─── */

export type ApiKeyScope =
  | 'agents:read'
  | 'agents:invoke'
  | 'workflows:read'
  | 'workflows:invoke'
  | 'executions:read'

export const ALL_API_KEY_SCOPES: ApiKeyScope[] = [
  'agents:read',
  'agents:invoke',
  'workflows:read',
  'workflows:invoke',
  'executions:read',
]

/** Human-readable scope labels for the create-form checkboxes. */
export const SCOPE_LABELS: Record<ApiKeyScope, string> = {
  'agents:read': '智能体·读取',
  'agents:invoke': '智能体·调用',
  'workflows:read': '工作流·读取',
  'workflows:invoke': '工作流·调用',
  'executions:read': '执行记录·读取',
}

export type ApiKeyStatus = 'active' | 'revoked'

/* ─── Types (snake_case, matches backend schemas) ─── */

export interface ApiKeyBindings {
  agents: string[]
  workflows: string[]
}

export interface ApiKeyItem {
  id: string
  name: string
  key_prefix: string
  owner_user_id: string
  scopes: ApiKeyScope[]
  bindings: ApiKeyBindings
  rate_limit: number
  status: ApiKeyStatus
  expires_at: string | null
  last_used_at: string | null
  user_info_url: string
  created_at: string
  updated_at: string
}

export interface ApiKeyCreateResponse extends Omit<ApiKeyItem, 'last_used_at' | 'updated_at'> {
  /** Raw key, returned exactly once on creation (e.g. af_live_xxx). */
  key: string
}

export interface ApiKeyCreatePayload {
  name: string
  scopes: ApiKeyScope[]
  bindings?: ApiKeyBindings
  rate_limit?: number
  /** ISO datetime or null (null = never expires). Defaults to null. */
  expires_at?: string | null
  /**
   * Introspection endpoint (RFC 7662). Empty/null = legacy mode;
   * set = callback mode (X-User-Token required per request).
   */
  user_info_url?: string | null
}

export interface ApiKeyListResponse {
  items: ApiKeyItem[]
  total: number
  page: number
  page_size: number
}

/* ─── API methods ─── */

export const apiKeysApi = {
  /**
   * List API Keys owned by the current user.
   * GET /api/v1/api-keys
   */
  async list(page = 1, pageSize = 50): Promise<ApiKeyListResponse> {
    const res = await apiClient.get<ApiKeyListResponse>('/api/v1/api-keys', {
      params: { page, page_size: pageSize },
    })
    return res.data
  },

  /**
   * Create a new API Key. The response includes the raw key once.
   * POST /api/v1/api-keys
   */
  async create(payload: ApiKeyCreatePayload): Promise<ApiKeyCreateResponse> {
    const res = await apiClient.post<ApiKeyCreateResponse>('/api/v1/api-keys', payload)
    return res.data
  },

  /**
   * Revoke (soft-delete) an API Key. Outstanding requests with it will fail.
   * DELETE /api/v1/api-keys/{id}
   */
  async revoke(apiKeyId: string): Promise<void> {
    await apiClient.delete(`/api/v1/api-keys/${encodeURIComponent(apiKeyId)}`)
  },
}

/* ─── Query key factory ─── */

export const apiKeyKeys = {
  all: ['api-keys'] as const,
  lists: () => [...apiKeyKeys.all, 'list'] as const,
  list: () => [...apiKeyKeys.lists()] as const,
}

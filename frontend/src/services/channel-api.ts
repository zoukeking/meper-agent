/**
 * Channel API service — wraps backend /channels endpoints.
 *
 * Manages IM platform (Lark / DingTalk / WeCom) channels and binds them
 * to Agents. Uses the shared apiClient instance (auto auth header + 401
 * refresh). Response fields are snake_case per backend contract.
 *
 * Follows the mcp-api.ts pattern: snake_case types, shared apiClient,
 * channelKeys query-key factory.
 */
import { apiClient } from './api-client'

/* ─── Types (snake_case, matches backend schemas) ─── */

export type ChannelProvider = 'lark' | 'dingtalk' | 'wecom' | 'mock'
export type ChannelStatus = 'active' | 'degraded' | 'disabled'
export type ReceiveMode = 'webhook' | 'long_connection'
export type ConnectionStatus =
  | 'long_connection_connected'
  | 'long_connection_disconnected'
  | 'not_long_connection'

export interface CredentialFieldSchema {
  key: string
  label: string
  type: 'text' | 'secret'
  required: boolean
}

export interface ProviderSchema {
  label: string
  credential_fields: CredentialFieldSchema[]
  /** Modes the provider supports at runtime (webhook always; long_connection
   * only if a ConnectionClient factory is registered server-side AND the
   * global enable flag is on). */
  receive_modes: ReceiveMode[]
}

export interface ProviderSchemaResponse {
  providers: Record<ChannelProvider, ProviderSchema>
}

export interface Channel {
  id: string
  name: string
  provider: ChannelProvider
  agent_id: string
  owner_user_id: string
  enabled: boolean
  status: ChannelStatus
  receive_mode: ReceiveMode
  /** Always masked on read (server-side). */
  credentials: Record<string, string>
  inbound_url: string
  /** Live state of the long-connection client (if in long_connection mode). */
  connection_status: ConnectionStatus
  created_at: string
  updated_at: string
}

export interface ChannelCreateInput {
  name: string
  provider: ChannelProvider
  agent_id: string
  credentials: Record<string, string>
  receive_mode?: ReceiveMode
}

export interface ChannelUpdateInput {
  name?: string
  agent_id?: string
  credentials?: Record<string, string>
  enabled?: boolean
  receive_mode?: ReceiveMode
}

export interface ChannelListParams {
  page?: number
  page_size?: number
}

export interface ChannelListResponse {
  items: Channel[]
  total: number
  page: number
  page_size: number
}

/* ─── API methods ─── */

export const channelApi = {
  /**
   * List channels with optional pagination.
   * GET /api/v1/channels
   */
  async list(params: ChannelListParams = {}): Promise<ChannelListResponse> {
    const res = await apiClient.get<ChannelListResponse>('/api/v1/channels', {
      params: {
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
      },
    })
    return res.data
  },

  /**
   * Get a single channel by ID.
   * GET /api/v1/channels/{id}
   */
  async get(channelId: string): Promise<Channel> {
    const res = await apiClient.get<Channel>(
      `/api/v1/channels/${encodeURIComponent(channelId)}`,
    )
    return res.data
  },

  /**
   * Create a new channel.
   * POST /api/v1/channels — returns 201 on success.
   */
  async create(input: ChannelCreateInput): Promise<Channel> {
    const res = await apiClient.post<Channel>('/api/v1/channels', input)
    return res.data
  },

  /**
   * Partially update a channel (name / agent / credentials / enabled).
   * PATCH /api/v1/channels/{id}
   */
  async update(channelId: string, input: ChannelUpdateInput): Promise<Channel> {
    const res = await apiClient.patch<Channel>(
      `/api/v1/channels/${encodeURIComponent(channelId)}`,
      input,
    )
    return res.data
  },

  /**
   * Delete a channel.
   * DELETE /api/v1/channels/{id} — returns 204.
   */
  async remove(channelId: string): Promise<void> {
    await apiClient.delete(`/api/v1/channels/${encodeURIComponent(channelId)}`)
  },

  /**
   * Enable a channel.
   * POST /api/v1/channels/{id}/enable
   */
  async enable(channelId: string): Promise<void> {
    await apiClient.post(`/api/v1/channels/${encodeURIComponent(channelId)}/enable`)
  },

  /**
   * Disable a channel.
   * POST /api/v1/channels/{id}/disable
   */
  async disable(channelId: string): Promise<void> {
    await apiClient.post(`/api/v1/channels/${encodeURIComponent(channelId)}/disable`)
  },

  /**
   * Reset a channel's degraded status back to active.
   * POST /api/v1/channels/{id}/reset
   */
  async reset(channelId: string): Promise<void> {
    await apiClient.post(`/api/v1/channels/${encodeURIComponent(channelId)}/reset`)
  },

  /**
   * Get provider credential field schemas for dynamic form rendering.
   * GET /api/v1/channels/providers/schema
   */
  async getProviderSchema(): Promise<ProviderSchemaResponse> {
    const res = await apiClient.get<ProviderSchemaResponse>(
      '/api/v1/channels/providers/schema',
    )
    return res.data
  },
}

/* ─── Query key factory ─── */

export const channelKeys = {
  all: ['channels'] as const,
  lists: () => [...channelKeys.all, 'list'] as const,
  list: (params: ChannelListParams) => [...channelKeys.lists(), params] as const,
  details: () => [...channelKeys.all, 'detail'] as const,
  detail: (id: string) => [...channelKeys.details(), id] as const,
  schema: () => [...channelKeys.all, 'schema'] as const,
}

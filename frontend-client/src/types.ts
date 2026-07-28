export interface AuthUser {
  id: string
  username: string
  role: string
  permissions: string[]
  is_super_admin?: boolean
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: AuthUser | null
}

export interface RecommendedItem {
  label: string
  prompt?: string | null
}

export interface AgentSummary {
  id: string
  name: string
  description: string
  avatar?: string | null
  status: string
  accessSource: 'company_owned' | 'platform_assigned'
  /** 终端用户首屏欢迎词（Markdown） */
  welcomeMessage?: string
  /** 终端用户首屏推荐问题/操作快捷项 */
  recommendedItems?: RecommendedItem[]
}

export interface EffectiveResource {
  resource_id: string
  name: string
  status: string
  details: Record<string, unknown>
  access_source: 'company_owned' | 'platform_assigned'
  effective_actions: string[]
}

export interface AgentRecord {
  id: string
  name: string
  description: string | null
  avatar?: string | null
  status: string
  welcome_message?: string | null
  recommended_items?: RecommendedItem[]
}

export interface ChatSession {
  id: string
  user_id: string
  agent_id: string
  title: string | null
  status: string
  created_at: string | null
}

export interface ToolCallMeta {
  tool_call_id: string
  name: string
  args?: string
  result?: string
  is_error?: boolean
  auto?: boolean
}

export interface ProcessMeta {
  reasoning?: string
  thoughts?: string[]
  tool_calls?: ToolCallMeta[]
}

export interface MessageRecord {
  id: string
  session_id: string
  role: string
  content: string
  timeline_entries?: Array<{
    type: string
    content?: string
    tool_name?: string
    args?: Record<string, unknown>
  }>
  files?: Array<{
    id?: string
    _id?: string
    name: string
    mime_type: string
    size?: number
  }>
  created_at: string | null
}

export interface ToolRun {
  id: string
  name: string
  args?: string
  result?: string
  isError?: boolean
  auto?: boolean
  status: 'running' | 'complete' | 'error'
}

export interface AttachmentView {
  id: string
  name: string
  contentType: string
  url?: string
  kind: 'image' | 'file'
  source: 'upload' | 'output' | 'local'
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  reasoning: string
  tools: ToolRun[]
  attachments: AttachmentView[]
  charts: string[]
  status: 'loading' | 'success' | 'error' | 'abort'
  createdAt?: Date
  error?: string
}

export interface HitlState {
  taskId: string
  question: string
  clarificationType: string
  context?: string
  options: string[]
}

export interface SessionFile {
  id?: string
  name?: string
  path: string
  size: number
  mime?: string
  is_output?: boolean
  modified: number | null
}

export interface FileUploadResult {
  id: string
  name: string
  path: string
  size: number
  mime: string
  is_output: boolean
}

export type StreamEventType =
  | 'text'
  | 'text_delta'
  | 'thinking'
  | 'thinking_delta'
  | 'tool_call_start'
  | 'tool_call'
  | 'tool_result'
  | 'interrupt'
  | 'error'

export interface StreamEvent {
  type?: StreamEventType
  done?: true
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  auto?: boolean
  question?: string
  clarification_type?: string
  context?: string | null
  options?: string[] | null
  interrupt_id?: string
  status?: 'success' | 'error'
  source?: 'llm' | 'tool' | 'graph'
}

export interface Agent {
  id: string;
  name: string;
  avatar: string;
  description: string;
  /** 终端用户首屏欢迎词（Markdown）— 后端 welcome_message */
  welcomeMessage?: string;
  /** 终端用户首屏推荐问题/操作 — 后端 recommended_items */
  recommendedItems?: { label: string; prompt: string }[];
  model: string;
  temperature: number;
  systemPrompt: string;
  /** 角色定义 — 后端 prompt_slots.role（必填） */
  rolePrompt: string;
  /** 任务描述 — 后端 prompt_slots.task（必填） */
  taskPrompt: string;
  /** 约束规则 — prompt_slots.constraints（可选） */
  constraintsPrompt: string;
  /** 上下文信息 — prompt_slots.context（可选） */
  contextPrompt: string;
  /** 输出格式 — prompt_slots.output_format（可选） */
  outputFormatPrompt: string;
  status: 'idle' | 'thinking' | 'online' | 'offline';
  statusText?: string;
  iconColor: string;
  skills: string[];
  lastActive: string;
  /** Max retry count for execution (backend: 0-10, default 3). */
  maxRetry?: number;
  /** Session token budget (backend max_tokens; 0 = use global default). */
  maxTokens?: number;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  category: 'all' | 'media' | 'finance' | 'legal' | 'tech' | 'edu' | 'health' | 'life' | 'common' | 'others';
  tags: string[];
  author: string;
  icon: string;
  iconColor: string;
  rating: number;
  usersCount: number;
  isAdded: boolean;
  isPaid?: boolean;
  includedSkills?: string[];
  datasets?: string[];
}

export interface WorkflowNode {
  id: string;
  type: 'start' | 'agent' | 'router' | 'checker' | 'end' | 'custom';
  label: string;
  x: number;
  y: number;
  systemPrompt?: string;
  model?: string;
  tools: string[];
  status: 'idle' | 'running' | 'completed' | 'failed';
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  status: 'idle' | 'active' | 'passed';
}

export interface Workflow {
  id: string;
  name: string;
  description: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  status: 'stable' | 'draft';
  lastRun?: string;
}

export interface User {
  id: string;
  name: string;
  avatar: string;
  email: string;
  /** 后端角色 name（key，如 admin / content_editor）。直存以兼容自定义角色，
   *  显示名由 UserManagement 从动态 roles 列表解析。 */
  role: string;
  permissions: {
    'agent:write': boolean;
    'workflow:write': boolean;
    'skill:write': boolean;
    'apikey:manage': boolean;
    'user:manage': boolean;
  };
  status: 'active' | 'suspended';
}

export interface ExecutionLog {
  id: string;
  workflowId: string;
  workflowName: string;
  timestamp: string;
  status: 'success' | 'failed' | 'running';
  latency: number;
  tokensUsed: number;
  steps: {
    nodeId: string;
    nodeLabel: string;
    timestamp: string;
    output: string;
  }[];
}

/** Token usage for an agent message (mirrors backend token_usage / SSE done usage). */
export interface TokenUsage {
  total_tokens?: number;
  input_tokens?: number;
  output_tokens?: number;
  llm_calls?: number;
  tool_calls?: number;
}

/** Timeline entry types for agent messages (mirrors frontend chat-panel). */
export type TimelineEntryType = 'thinking' | 'tool' | 'text' | 'error';
export type ToolStatus = 'pending' | 'running' | 'success' | 'error';

/** A single entry in an agent message's timeline. Agent messages render as an
 *  ordered list of these entries (text / tool / thinking / error), so multi-
 *  turn REACT (text→tool→text→tool) keeps correct chronological order. */
export interface TimelineEntry {
  id: string;
  type: TimelineEntryType;
  /** text/error/thinking 的正文 */
  content: string;
  /** tool 类型专用 */
  toolName?: string;
  args?: Record<string, unknown>;
  result?: string;
  toolStatus?: ToolStatus;
  /** 折叠态（thinking / tool 详情） */
  expanded?: boolean;
}

export interface Message {
  id: string;
  senderName: string;
  avatar: string;
  role: 'user' | 'agent';
  agentId?: string;
  content: string;
  timestamp: string;
  status?: string;
  attachment?: {
    name: string;
    type: 'markdown' | 'video' | 'image' | 'code';
    content: string;
  };
  /** 可预览附件列表（用户上传 + agent 产出统一抽象）。
   *  source 决定取数路径：upload 走 getFileBlob(ref)（FileRef.id），
   *  output 走 sessionApi.previewFile(sid, ref)（output/ 相对路径）。 */
  attachments?: ChatAttachment[];
  /** Agent 消息的 timeline（text/tool/thinking/error 按顺序）。
   *  agent 消息靠此字段渲染；扁平 tool* 字段仅过渡期保留。 */
  timeline?: TimelineEntry[];
  /** Agent 经 interrupt 暂停，等待用户回答（ask_clarification /
   *  confirm_workflow）。为 true 时，timeline 里未答的 tool entry 卡片
   *  可交互，下一次发送走 /resume。 */
  isInterrupted?: boolean;
  /** Structured tool-call fields. When `status === 'tool'`, the message is
   *  rendered as a ToolCallCard instead of a plain text bubble. (过渡期保留) */
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolResult?: string;
  toolStatus?: ToolStatus;
  /** ask_clarification 等待用户回答时为 true（卡片可交互，回答走 /resume）。
   *  (过渡期保留，新代码用 isInterrupted + timeline entry) */
  clarificationActive?: boolean;
  /** Token usage for this agent message (history: rec.token_usage; stream: done event usage). */
  usage?: TokenUsage;
}

/** 对话内可预览附件（用户上传 / agent 产出统一模型）。
 *  - source='upload'：ref = FileRef.id，经 getFileBlob(ref) 取 blob
 *  - source='output'：ref = 相对 output/ 的路径，经 sessionApi.previewFile(sid, ref) 取 blob */
export interface ChatAttachment {
  source: 'upload' | 'output';
  ref: string;
  name: string;
  mime_type?: string;
  size?: number;
}

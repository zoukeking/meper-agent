/**
 * Adapters — map between studio view models (src/types.ts) and backend
 * snake_case models (services/*-api.ts).
 *
 * Backend contract reference: backend/app/schemas/*.py
 * Studio view model: src/types.ts
 *
 * NOTE (gap): backend Tools have no store metadata (rating / category /
 * isAdded / isPaid / includedSkills / datasets). Those fields stay
 * client-side (local state in SkillsStore) and are documented as gaps in
 * features/active-feat-studio-core-crud/spec.md.
 */
import type { Agent as BackendAgent } from './agent-api'
import type { Tool } from './tools-api'
import type { McpConnection } from './mcp-api'
import type { User as BackendUser } from './user-api'
import type { Agent, Skill, User } from '../types'

/* ──────────────────────── Agent ──────────────────────── */

const DEFAULT_AGENT_AVATAR = '🤖'
const DEFAULT_AGENT_ICON_COLOR = 'bg-indigo-500'
const DEFAULT_AGENT_TEMPERATURE = 0.5

/**
 * Map backend AgentStatus → studio display status.
 * Studio's Agent.status is a richer display union ('idle'|'thinking'|
 * 'online'|'offline'); we collapse the 3 lifecycle states onto it.
 */
function agentStatusToDisplay(status: BackendAgent['status']): Agent['status'] {
  switch (status) {
    case 'published':
      return 'online'
    case 'archived':
      return 'offline'
    case 'draft':
    default:
      return 'idle'
  }
}

/** Convert a studio display status back to a backend lifecycle status. */
export function displayToAgentStatus(s: Agent['status']): BackendAgent['status'] {
  switch (s) {
    case 'online':
      return 'published'
    case 'offline':
      return 'archived'
    case 'idle':
    case 'thinking':
    default:
      return 'draft'
  }
}

/** Backend Agent → studio Agent view model. */
export function toStudioAgent(a: BackendAgent): Agent {
  const skillsFromSkillIds = [...(a.skill_ids ?? [])]
  const skillsFromMcp = (a.mcp_connection_ids ?? []).map((id) => `mcp:${id}`)
  const skillsFromBuiltin = (a.builtin_config ?? []).map((b) => `builtin:${b}`)
  const skillsFromWorkflows = (a.workflow_ids ?? []).map((id) => `workflow:${id}`)
  const skillsFromKb = (a.knowledge_base_ids ?? []).map((id) => `kb:${id}`)
  return {
    id: a.id,
    name: a.name,
    avatar: DEFAULT_AGENT_AVATAR,
    description: a.description ?? '',
    welcomeMessage: a.welcome_message ?? '',
    recommendedItems: (a.recommended_items ?? []).map((it) => ({
      label: it.label ?? '',
      prompt: it.prompt ?? '',
    })),
    model: a.default_model || 'gemini-3.5-flash',
    temperature: DEFAULT_AGENT_TEMPERATURE,
    // role/task map to backend prompt_slots.role/.task (both required by slot_renderer).
    // systemPrompt kept as a legacy fallback (prompt_slots.system) for old agents.
    rolePrompt: a.prompt_slots?.role ?? '',
    taskPrompt: a.prompt_slots?.task ?? '',
    constraintsPrompt: a.prompt_slots?.constraints ?? '',
    contextPrompt: a.prompt_slots?.context ?? '',
    outputFormatPrompt: a.prompt_slots?.output_format ?? '',
    systemPrompt: a.prompt_slots?.system ?? a.prompt_slots?.system_prompt ?? '',
    status: agentStatusToDisplay(a.status),
    iconColor: DEFAULT_AGENT_ICON_COLOR,
    skills: [...skillsFromSkillIds, ...skillsFromMcp, ...skillsFromBuiltin, ...skillsFromWorkflows, ...skillsFromKb],
    lastActive: a.updated_at ? new Date(a.updated_at).toLocaleString() : '—',
    maxRetry: a.max_retry ?? 3,
    maxTokens: a.max_tokens ?? 0,
  }
}

/** Studio Agent edit form → backend AgentUpdateInput (partial). */
export function fromStudioAgent(a: Agent): {
  name: string
  description?: string
  welcome_message?: string
  recommended_items?: { label: string; prompt: string }[]
  prompt_slots?: Record<string, string>
  default_model?: string
  skill_ids?: string[]
  mcp_connection_ids?: string[]
  builtin_config?: string[]
  workflow_ids?: string[]
  knowledge_base_ids?: string[]
  max_retry?: number
  max_tokens?: number
} {
  // Split skill view-model strings back into their origin buckets.
  const skill_ids: string[] = []
  const mcp_connection_ids: string[] = []
  const builtin_config: string[] = []
  const workflow_ids: string[] = []
  const knowledge_base_ids: string[] = []
  for (const s of a.skills ?? []) {
    if (s.startsWith('mcp:')) mcp_connection_ids.push(s.slice(4))
    else if (s.startsWith('builtin:')) builtin_config.push(s.slice(8))
    else if (s.startsWith('workflow:')) workflow_ids.push(s.slice(9))
    else if (s.startsWith('kb:')) knowledge_base_ids.push(s.slice(3))
    else skill_ids.push(s)
  }
  return {
    name: a.name,
    description: a.description,
    welcome_message: a.welcomeMessage ?? '',
    recommended_items: (a.recommendedItems ?? []).map((it) => ({
      label: it.label,
      prompt: it.prompt ?? '',
    })),
    // Backend slot_renderer requires role + task; system is kept for legacy compat.
    prompt_slots: {
      role: a.rolePrompt ?? '',
      task: a.taskPrompt ?? '',
      ...(a.constraintsPrompt ? { constraints: a.constraintsPrompt } : {}),
      ...(a.contextPrompt ? { context: a.contextPrompt } : {}),
      ...(a.outputFormatPrompt ? { output_format: a.outputFormatPrompt } : {}),
      ...(a.systemPrompt ? { system: a.systemPrompt } : {}),
    },
    default_model: a.model,
    skill_ids,
    mcp_connection_ids,
    builtin_config,
    ...(workflow_ids.length ? { workflow_ids } : {}),
    ...(knowledge_base_ids.length ? { knowledge_base_ids } : {}),
    max_retry: a.maxRetry ?? 3,
    max_tokens: a.maxTokens ?? 0,
  }
}

/* ──────────────────────── Skill (Tool) ──────────────────────── */

const SKILL_CATEGORY_FALLBACK: Skill['category'] = 'others'

/**
 * Backend Tool (source=markdown/mcp/builtin) → studio Skill view model.
 * Store-only metadata (rating/category/isAdded/isPaid/includedSkills/
 * datasets) is seeded with neutral defaults and managed client-side — see
 * spec gap note.
 */
export function toStudioSkill(t: Tool): Skill {
  return {
    id: t.id,
    name: t.name,
    description: t.description ?? t.instructions ?? '',
    // backend has no category; bucket by source, fallback to 'others'
    category: sourceToCategory(t.source),
    tags: t.tags?.length ? t.tags : sourceToTags(t.source),
    author: t.source === 'mcp' ? `mcp:${t.mcp_connection_id ?? ''}` : (t.source ?? 'system'),
    icon: t.source === 'mcp' ? '🔌' : t.source === 'builtin' ? '⚙️' : '📘',
    iconColor: t.source === 'mcp'
      ? 'from-amber-600 to-indigo-600'
      : 'from-indigo-600 to-purple-600',
    rating: 0,
    usersCount: 0,
    isAdded: true, // a persisted Tool is by definition "added"
  }
}

function sourceToCategory(source: string): Skill['category'] {
  switch (source) {
    case 'mcp':
      return 'tech'
    case 'builtin':
      return 'common'
    case 'markdown':
      return 'tech'
    default:
      return SKILL_CATEGORY_FALLBACK
  }
}

function sourceToTags(source: string): string[] {
  switch (source) {
    case 'mcp':
      return ['MCP 拓展', '开发工具']
    case 'builtin':
      return ['内置工具']
    default:
      return ['技能包']
  }
}

/** Map an MCP connection to a studio Skill entry (for the MCP registry list). */
export function mcpConnectionToSkill(c: McpConnection): Skill {
  return {
    id: c.id,
    name: c.name,
    description: c.description ?? c.status_message ?? '',
    category: 'tech',
    tags: ['MCP 拓展', '开发工具'],
    author: c.url,
    icon: '🔌',
    iconColor: 'from-amber-600 to-indigo-600',
    rating: 0,
    usersCount: c.tool_count ?? 0,
    isAdded: c.status === 'connected',
  }
}

/* ──────────────────────── User ──────────────────────── */

/**
 * Backend stores `role` as the role's `name` (lowercase key, e.g. 'admin',
 * 'content_editor'). Studio mirrors that key verbatim in User.role; the
 * display name is resolved from the roles list at render time (see
 * UserManagement). No mapping table is needed - custom roles pass through
 * unchanged instead of being collapsed to a system role.
 */

/**
 * The 5 coarse permission buckets the studio UI toggles, mapped to the
 * fine-grained backend permission keys (24 total, see services/types.ts
 * PERMISSION_GROUPS). A bucket is "on" iff the user has any key in it.
 */
const COARSE_PERM_BUCKETS = {
  'agent:write': ['agent:write'],
  'workflow:write': ['workflow:write'],
  'skill:write': ['skill:write', 'tool:write', 'mcp:write'],
  'apikey:manage': ['apikey:manage'],
  'user:manage': ['user:write'],
} as const

export type CoarsePermKey = keyof typeof COARSE_PERM_BUCKETS

/** Backend permissions[] → 5-bucket coarse booleans for the studio UI. */
export function permissionsToCoarse(
  perms: string[],
): User['permissions'] {
  const set = new Set(perms ?? [])
  return {
    'agent:write': COARSE_PERM_BUCKETS['agent:write'].some((p) => set.has(p)),
    'workflow:write': COARSE_PERM_BUCKETS['workflow:write'].some((p) => set.has(p)),
    'skill:write': COARSE_PERM_BUCKETS['skill:write'].some((p) => set.has(p)),
    'apikey:manage': COARSE_PERM_BUCKETS['apikey:manage'].some((p) => set.has(p)),
    'user:manage': COARSE_PERM_BUCKETS['user:manage'].some((p) => set.has(p)),
  }
}

/**
 * Coarse 5-bucket booleans → expanded backend permission keys.
 * Expanding a bucket turns ON every fine key inside it; turning a bucket
 * OFF removes every fine key inside it (so toggling is idempotent).
 */
export function coarseToPermissions(
  coarse: User['permissions'],
  existing: string[] = [],
): string[] {
  const result = new Set(existing.filter(Boolean))
  for (const bucket of Object.keys(COARSE_PERM_BUCKETS) as CoarsePermKey[]) {
    const on = coarse[bucket]
    for (const key of COARSE_PERM_BUCKETS[bucket]) {
      if (on) result.add(key)
      else result.delete(key)
    }
  }
  return [...result]
}

/**
 * Default coarse permissions for a role key. Used only for the advisory
 * permission-bucket notice in UserManagement (real perms live on the role,
 * not the user). System roles map to a fixed bucket set keyed by backend
 * name; custom roles default to all-off.
 */
export function defaultCoarseForRole(role: string): User['permissions'] {
  return {
    'agent:write': role === 'admin' || role === 'developer',
    'workflow:write': role === 'admin' || role === 'developer',
    'skill:write': role === 'admin' || role === 'developer' || role === 'operator',
    'apikey:manage': role === 'admin',
    'user:manage': role === 'admin',
  }
}

/** Backend User → studio User view model. */
export function toStudioUser(u: BackendUser): User {
  return {
    id: u.id,
    name: u.username,
    avatar: ['👨‍💻', '👩‍💻', '👩‍💼', '🧑‍🔬', '🧙‍♂️'][Math.floor(Math.random() * 5)],
    email: u.email,
    role: u.role,
    permissions: permissionsToCoarse(u.permissions),
    status: u.status === 'active' ? 'active' : 'suspended',
  }
}

/* NOTE: The old 5-column TaskBoard mapping (BoardColumn / BOARD_COLUMNS /
 * taskStatusToBoardColumn / columnToInterveneAction) lived here as a best-effort
 * status→column adapter with a documented GAP. The board was rewritten to map
 * 1:1 onto the 6 backend statuses directly (see constants/task-status.ts and
 * components/TaskBoard.tsx), so these helpers are no longer referenced and have
 * been removed. */


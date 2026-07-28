/**
 * ToolSelector — 四段式工具选择器组件。
 *
 * 将 Agent 工具配置拆分为四个分类：
 *  1. Built-in 工具（可配的文件类工具 + 始终开启的能力型工具）— 动态拉取自 /tools/builtin
 *  2. Skill 工具（source=markdown 的上传技能）— Switch 开关列表
 *  3. MCP 连接（远程工具服务器）— Switch 开关列表
 *  4. 工作流（Workflow 模板）— Switch 开关列表
 *
 * 作为受控组件使用，value/onChange 接收/返回统一的 ToolSelectorValue。
 */
import { useQuery } from '@tanstack/react-query'
import { Switch, Skeleton, Typography, Alert, Tag } from 'antd'
import {
  CodeOutlined,
  ApiOutlined,
  ThunderboltOutlined,
  ApartmentOutlined,
  ToolOutlined,
  LockOutlined,
} from '@ant-design/icons'
import { toolsApi, toolKeys } from '../services/tools-api'
import { mcpApi, mcpKeys } from '../services/mcp-api'
import { workflowsApi, workflowKeys } from '../services/workflows-api'

const { Text } = Typography

/** MCP 连接状态中文映射 */
const MCP_STATUS_LABELS: Record<string, string> = {
  connecting: '连接中',
  connected: '已连接',
  disconnected: '已断开',
  error: '异常',
}

/* ─── Value 类型 ─── */
export interface CustomToolBinding {
  tool_id: string
  user_args: Record<string, string>
}

export interface ToolSelectorValue {
  builtin_config: string[]
  skill_ids: string[]
  mcp_connection_ids: string[]
  workflow_ids: string[]
  custom_tool_ids: string[]
}

// eslint-disable-next-line react-refresh/only-export-components
export const DEFAULT_TOOL_VALUE: ToolSelectorValue = {
  // 文件类内建工具默认全开(与后端 create_agent 端点的默认值保持一致,
  // 避免新建后工具面板的勾选状态与落库值不一致导致视觉跳变)。
  builtin_config: ['bash', 'read', 'write', 'glob', 'grep'],
  skill_ids: [],
  mcp_connection_ids: [],
  workflow_ids: [],
  custom_tool_ids: [],
}

/* ─── Props ─── */
export interface ToolSelectorProps {
  value?: ToolSelectorValue
  onChange?: (value: ToolSelectorValue) => void
  /** 是否正在加载（父表单编辑态初始化时使用） */
  loading?: boolean
}

/**
 * 合并当前值与 partial update，返回新对象。
 */
function mergeValue(
  prev: ToolSelectorValue,
  patch: Partial<ToolSelectorValue>,
): ToolSelectorValue {
  return { ...prev, ...patch }
}

export default function ToolSelector({
  value = DEFAULT_TOOL_VALUE,
  onChange,
  loading = false,
}: ToolSelectorProps) {
  /* ─── 数据请求 ─── */
  const { data: builtinsData, isLoading: builtinsLoading, isError: builtinsError } = useQuery({
    queryKey: toolKeys.builtins(),
    queryFn: () => toolsApi.listBuiltins(),
  })

  const { data: skillsData, isLoading: skillsLoading, isError: skillsError } = useQuery({
    queryKey: toolKeys.list({ page: 1, page_size: 100, source: 'markdown' }),
    queryFn: () => toolsApi.list({ page: 1, page_size: 100, source: 'markdown' }),
  })

  const { data: mcpData, isLoading: mcpLoading, isError: mcpError } = useQuery({
    queryKey: mcpKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => mcpApi.list({ page: 1, page_size: 100 }),
  })

  const { data: wfData, isLoading: wfLoading, isError: wfError } = useQuery({
    queryKey: workflowKeys.list({ page: 1, page_size: 100, status: 'published' }),
    queryFn: () => workflowsApi.list({ page: 1, page_size: 100, status: 'published' }),
  })

  const availableSkills = skillsData?.items ?? []
  const availableMcpConnections = mcpData?.items ?? []
  const availableWorkflows = wfData?.items ?? []

  /* Built-in 工具拆分:可配的文件类 vs 始终开启的能力型 */
  const allBuiltins = builtinsData ?? []
  const configurableBuiltins = allBuiltins.filter((t) => t.configurable !== false)
  const alwaysOnBuiltins = allBuiltins.filter((t) => t.configurable === false)

  /* ─── Loading ─── */
  if (loading) {
    return (
      <div className="flex flex-col gap-4 p-4">
        <Skeleton active paragraph={{ rows: 1 }} />
        <Skeleton active paragraph={{ rows: 1 }} />
        <Skeleton active paragraph={{ rows: 1 }} />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-5">
      {/* ────────── Built-in 工具 ────────── */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <ThunderboltOutlined className="text-[#F59E0B] text-base" />
          <Text strong className="text-sm">
            Built-in 工具
          </Text>
          <Text className="text-[11px] text-[#94A3B8]">
            （{value.builtin_config.length}/{configurableBuiltins.length} 已启用）
          </Text>
        </div>
        {builtinsError ? (
          <Alert message="加载内建工具失败" type="error" showIcon className="!rounded-lg" />
        ) : builtinsLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : (
          <div className="max-h-[180px] overflow-y-auto border border-[#E2E8F0] rounded-lg divide-y divide-[#E2E8F0]">
            {configurableBuiltins.map((tool) => {
              const enabled = value.builtin_config.includes(tool.name)
              return (
                <div
                  key={tool.name}
                  className="flex items-center justify-between px-3 py-2 hover:bg-[#F8FAFC] transition-colors"
                >
                  <span className="text-sm text-[#0F172A] truncate pr-2">{tool.name}</span>
                  <Switch
                    size="small"
                    checked={enabled}
                    onChange={(checked) => {
                      const next = checked
                        ? [...value.builtin_config, tool.name]
                        : value.builtin_config.filter((n) => n !== tool.name)
                      onChange?.(mergeValue(value, { builtin_config: next }))
                    }}
                  />
                </div>
              )
            })}
            {alwaysOnBuiltins.map((tool) => (
              <div
                key={tool.name}
                className="flex items-center justify-between px-3 py-2 bg-[#F8FAFC]"
              >
                <div className="flex items-center gap-1.5 min-w-0 pr-2">
                  <LockOutlined className="text-[#94A3B8] text-xs shrink-0" />
                  <span className="text-sm text-[#0F172A] truncate">{tool.name}</span>
                </div>
                <Switch size="small" checked disabled />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ────────── Skill 工具 ────────── */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <CodeOutlined className="text-[#3B82F6] text-base" />
          <Text strong className="text-sm">
            Skill 工具
          </Text>
          <Text className="text-[11px] text-[#94A3B8]">
            （{value.skill_ids.length}/{availableSkills.length} 已启用）
          </Text>
        </div>
        {skillsError ? (
          <Alert message="加载 Skill 列表失败" type="error" showIcon className="!rounded-lg" />
        ) : skillsLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : (
          <div className="max-h-[180px] overflow-y-auto border border-[#E2E8F0] rounded-lg divide-y divide-[#E2E8F0]">
            {availableSkills.length === 0 ? (
              <div className="px-3 py-3 text-center text-[11px] text-[#94A3B8]">
                暂无可用 Skill，请先在工具中心上传
              </div>
            ) : availableSkills.map((s) => {
              const enabled = value.skill_ids.includes(s.id)
              return (
                <div
                  key={s.id}
                  className="flex items-center justify-between px-3 py-2 hover:bg-[#F8FAFC] transition-colors"
                >
                  <span className="text-sm text-[#0F172A] truncate pr-2">{s.name}</span>
                  <Switch
                    size="small"
                    checked={enabled}
                    onChange={(checked) => {
                      const next = checked
                        ? [...value.skill_ids, s.id]
                        : value.skill_ids.filter((id) => id !== s.id)
                      onChange?.(mergeValue(value, { skill_ids: next }))
                    }}
                  />
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ────────── MCP 连接 ────────── */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <ApiOutlined className="text-[#10B981] text-base" />
          <Text strong className="text-sm">
            MCP 连接
          </Text>
          <Text className="text-[11px] text-[#94A3B8]">
            （{value.mcp_connection_ids.length}/{availableMcpConnections.length} 已启用）
          </Text>
        </div>
        {mcpError ? (
          <Alert message="加载 MCP 连接列表失败" type="error" showIcon className="!rounded-lg" />
        ) : mcpLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : (
          <div className="max-h-[180px] overflow-y-auto border border-[#E2E8F0] rounded-lg divide-y divide-[#E2E8F0]">
            {availableMcpConnections.length === 0 ? (
              <div className="px-3 py-3 text-center text-[11px] text-[#94A3B8]">
                暂无 MCP 连接，请先在 MCP 页面配置
              </div>
            ) : availableMcpConnections.map((c) => {
              const enabled = value.mcp_connection_ids.includes(c.id)
              const statusLabel = MCP_STATUS_LABELS[c.status] ?? c.status
              return (
                <div
                  key={c.id}
                  className="flex items-center justify-between px-3 py-2 hover:bg-[#F8FAFC] transition-colors"
                >
                  <div className="flex items-center gap-2 min-w-0 pr-2">
                    <span className="text-sm text-[#0F172A] truncate">{c.name}</span>
                    <span className={`text-[11px] shrink-0 ${
                      c.status === 'connected' ? 'text-[#10B981]' :
                      c.status === 'error' ? 'text-[#EF4444]' :
                      'text-[#94A3B8]'
                    }`}>
                      {statusLabel}
                    </span>
                  </div>
                  <Switch
                    size="small"
                    checked={enabled}
                    onChange={(checked) => {
                      const next = checked
                        ? [...value.mcp_connection_ids, c.id]
                        : value.mcp_connection_ids.filter((id) => id !== c.id)
                      onChange?.(mergeValue(value, { mcp_connection_ids: next }))
                    }}
                  />
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ────────── Workflow ────────── */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <ApartmentOutlined className="text-[#F97316] text-base" />
          <Text strong className="text-sm">
            工作流
          </Text>
          <Text className="text-[11px] text-[#94A3B8]">
            （{value.workflow_ids.length}/{availableWorkflows.length} 已启用）
          </Text>
        </div>
        {wfError ? (
          <Alert message="加载工作流列表失败" type="error" showIcon className="!rounded-lg" />
        ) : wfLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : (
          <div className="max-h-[180px] overflow-y-auto border border-[#E2E8F0] rounded-lg divide-y divide-[#E2E8F0]">
            {availableWorkflows.length === 0 ? (
              <div className="px-3 py-3 text-center text-[11px] text-[#94A3B8]">
                暂无已发布的工作流，请先在工作流页面创建并发布
              </div>
            ) : availableWorkflows.map((wf) => {
              const enabled = value.workflow_ids.includes(wf.id)
              return (
                <div
                  key={wf.id}
                  className="flex items-center justify-between px-3 py-2 hover:bg-[#F8FAFC] transition-colors"
                >
                  <div className="flex items-center gap-2 min-w-0 pr-2">
                    <span className="text-sm text-[#0F172A] truncate">{wf.name}</span>
                    {wf.description && (
                      <span className="text-[11px] text-[#94A3B8] truncate max-w-[200px]">
                        {wf.description}
                      </span>
                    )}
                  </div>
                  <Switch
                    size="small"
                    checked={enabled}
                    onChange={(checked) => {
                      const next = checked
                        ? [...value.workflow_ids, wf.id]
                        : value.workflow_ids.filter((id) => id !== wf.id)
                      onChange?.(mergeValue(value, { workflow_ids: next }))
                    }}
                  />
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* ────────── Custom Tools ────────── */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <ToolOutlined className="text-[#8B5CF6] text-base" />
          <Text strong className="text-sm">
            自定义工具
          </Text>
          <Text className="text-[11px] text-[#94A3B8]">
            （{value.custom_tool_ids?.length || 0} 已启用）
          </Text>
        </div>
        <CustomToolSelector
          value={value.custom_tool_ids || []}
          onChange={(ids) => onChange?.(mergeValue(value, { custom_tool_ids: ids }))}
        />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Custom tool selector — queries openapi/code/prebuilt tools from DB
// ---------------------------------------------------------------------------

function CustomToolSelector({
  value,
  onChange,
}: {
  value: string[]
  onChange: (ids: string[]) => void
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: toolKeys.customTools(),
    queryFn: () =>
      toolsApi.list({ page: 1, page_size: 100, source: 'openapi' }).then(async (r1) => {
        // Also fetch code + prebuilt sources and merge
        const r2 = await toolsApi.list({ page: 1, page_size: 100, source: 'code' })
        const r3 = await toolsApi.list({ page: 1, page_size: 100, source: 'prebuilt' })
        return [...r1.items, ...r2.items, ...r3.items]
      }),
  })

  if (error) {
    return <Alert message="加载自定义工具失败" type="error" showIcon className="!rounded-lg" />
  }
  if (isLoading) {
    return <Skeleton active paragraph={{ rows: 2 }} />
  }

  const tools = data || []
  if (tools.length === 0) {
    return (
      <div className="border border-[#E2E8F0] rounded-lg px-3 py-3 text-center text-[11px] text-[#94A3B8]">
        暂无自定义工具，请先在工具页面创建（OpenAPI / Code / 预构建）
      </div>
    )
  }

  const SOURCE_TAGS: Record<string, { color: string; label: string }> = {
    openapi: { color: 'blue', label: 'API' },
    code: { color: 'green', label: 'Code' },
    prebuilt: { color: 'purple', label: 'Prebuilt' },
  }

  return (
    <div className="max-h-[180px] overflow-y-auto border border-[#E2E8F0] rounded-lg divide-y divide-[#E2E8F0]">
      {tools.map((tool) => {
        const enabled = value.includes(tool.id)
        const tag = SOURCE_TAGS[tool.source] || { color: 'default', label: tool.source }
        return (
          <div
            key={tool.id}
            className="flex items-center justify-between px-3 py-2 hover:bg-[#F8FAFC] transition-colors"
          >
            <div className="flex items-center gap-2 min-w-0 pr-2">
              <Tag color={tag.color} className="!text-[10px] !px-1.5 !py-0">{tag.label}</Tag>
              <span className="text-sm text-[#0F172A] truncate">{tool.name}</span>
              {tool.description && (
                <span className="text-[11px] text-[#94A3B8] truncate max-w-[200px]">
                  {tool.description}
                </span>
              )}
            </div>
            <Switch
              size="small"
              checked={enabled}
              onChange={(checked) => {
                const next = checked
                  ? [...value, tool.id]
                  : value.filter((id) => id !== tool.id)
                onChange(next)
              }}
            />
          </div>
        )
      })}
    </div>
  )
}

/**
 * Tools page — platform tools + custom tool market.
 *
 * Tab 1: 平台工具 (built-in + prebuilt, read-only)
 * Tab 2: 自定义工具 (user-created OpenAPI/Code tools, CRUD)
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Spin, Empty, Tag, Button, Modal, Tabs, message } from 'antd'
import type { TabsProps } from 'antd'
import {
  SearchOutlined, CodeOutlined, ReadOutlined, EditOutlined, ToolOutlined,
  PlusOutlined, GlobalOutlined, DeleteOutlined, SafetyOutlined,
  FileSearchOutlined, QuestionCircleOutlined,
  LockOutlined,
} from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import { toolsApi, toolKeys } from '../services/tools-api'
import { agentKeys } from '../services/agent-api'
import type { BuiltinTool, Tool } from '../services/tools-api'
import ToolCreateDrawer from '../components/tool-create-drawer'

const TOOL_ICONS: Record<string, typeof CodeOutlined> = {
  bash: CodeOutlined,
  read: ReadOutlined,
  write: EditOutlined,
  glob: FileSearchOutlined,
  grep: FileSearchOutlined,
  ask_clarification: QuestionCircleOutlined,
}

export default function ToolsPage() {
  const [activeTab, setActiveTab] = useState('platform')

  const tabItems: TabsProps['items'] = [
    { key: 'platform', label: '平台工具', children: <PlatformToolsTab /> },
    { key: 'custom', label: '自定义工具', children: <CustomToolsTab /> },
  ]

  return (
    <div className="p-6 animate-[fadeIn_0.3s_ease-out]">
      <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
    </div>
  )
}

// ===========================================================================
// Tab 1: Platform Tools (Built-in + Prebuilt)
// ===========================================================================

function PlatformToolsTab() {
  const { t } = useTheme()
  const [searchName, setSearchName] = useState('')

  const { data: builtins, isLoading: builtinsLoading } = useQuery({
    queryKey: toolKeys.builtins(),
    queryFn: () => toolsApi.listBuiltins(),
  })

  const { data: appTools, isLoading: appToolsLoading } = useQuery({
    queryKey: toolKeys.appTools(),
    queryFn: () => toolsApi.listAppTools(),
  })

  const { data: prebuilts, isLoading: prebuiltsLoading } = useQuery({
    queryKey: toolKeys.prebuilts(),
    queryFn: () => toolsApi.listPrebuilt(),
  })

  const matches = (tool: { name: string; description: string }) => {
    if (!searchName) return true
    const q = searchName.toLowerCase()
    return tool.name.toLowerCase().includes(q) || tool.description.toLowerCase().includes(q)
  }
  const filteredBuiltins = (builtins ?? []).filter(matches)
  const filteredAppTools = (appTools ?? []).filter(matches)

  return (
    <div>
      {/* Search */}
      <div className="flex items-center gap-3 mb-6">
        <div className="relative">
          <SearchOutlined className="absolute left-3 top-1/2 -translate-y-1/2 text-[#94A3B8] text-sm" />
          <input
            type="text" placeholder="搜索工具..." value={searchName}
            onChange={(e) => setSearchName(e.target.value)}
            className="pl-9 pr-4 py-2 rounded-lg border border-gray-200 text-sm focus:outline-none focus:ring-2 w-64"
            style={{ '--tw-ring-color': t.bg } as React.CSSProperties}
          />
        </div>
      </div>

      {/* Built-in tools section */}
      <div className="mb-8">
        <h3 className="text-sm font-semibold text-[#0F172A] mb-3 flex items-center gap-2">
          <ToolOutlined className="text-[#7C3AED]" /> 内建工具
        </h3>
        {builtinsLoading ? (
          <div className="flex justify-center py-8"><Spin /></div>
        ) : filteredBuiltins.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无" className="py-8" />
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {filteredBuiltins.map(tool => <BuiltinToolCard key={tool.name} tool={tool} />)}
          </div>
        )}
      </div>

      {/* App-level tools section (always-on task/workflow tools) */}
      <div className="mb-8">
        <h3 className="text-sm font-semibold text-[#0F172A] mb-3 flex items-center gap-2">
          <SafetyOutlined className="text-[#2563EB]" /> 应用工具
          <span className="text-[11px] font-normal text-[#94A3B8]">（始终开启，不可关闭）</span>
        </h3>
        {appToolsLoading ? (
          <div className="flex justify-center py-8"><Spin /></div>
        ) : filteredAppTools.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无" className="py-8" />
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {filteredAppTools.map(tool => <BuiltinToolCard key={tool.name} tool={tool} />)}
          </div>
        )}
      </div>

      {/* Prebuilt tools section */}
      <div>
        <h3 className="text-sm font-semibold text-[#0F172A] mb-3 flex items-center gap-2">
          <SafetyOutlined className="text-[#2563EB]" /> 预构建工具
        </h3>
        {prebuiltsLoading ? (
          <div className="flex justify-center py-8"><Spin /></div>
        ) : (prebuilts ?? []).length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span>暂无预构建工具<br />平台后续会注册 Wikipedia、Web Search 等预构建工具</span>}
            className="py-8"
          />
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {(prebuilts ?? []).map((tool, i) => <PrebuiltToolCard key={i} tool={tool} />)}
          </div>
        )}
      </div>
    </div>
  )
}

function BuiltinToolCard({ tool }: { tool: BuiltinTool }) {
  const { t } = useTheme()
  const Icon = TOOL_ICONS[tool.name] ?? ToolOutlined
  const paramNames = Object.keys((tool.parameters?.properties as Record<string, unknown>) ?? {})
  const alwaysOn = tool.configurable === false
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 hover:shadow-sm transition-all">
      <div className="flex items-start gap-3 mb-3">
        <div className="w-10 h-10 rounded-lg flex items-center justify-center text-base shrink-0"
          style={{ background: t.bg, color: t.primary }}>
          <Icon />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <div className="text-sm font-medium text-[#0F172A]">{tool.name}</div>
            {alwaysOn && (
              <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded !leading-4 flex items-center gap-0.5"
                style={{ color: '#94A3B8', background: '#F1F5F9', borderColor: 'transparent' }}>
                <LockOutlined className="!text-[9px]" />始终开启
              </Tag>
            )}
          </div>
          <div className="text-xs text-[#64748B] line-clamp-2">{tool.description}</div>
        </div>
      </div>
      <div className="pt-3 border-t border-gray-50">
        {paramNames.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {paramNames.map(p => (
              <Tag key={p} className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded"
                style={{ color: '#64748B', background: '#F1F5F9', borderColor: 'transparent' }}>{p}</Tag>
            ))}
          </div>
        ) : <span className="text-xs text-[#94A3B8]">无参数</span>}
      </div>
    </div>
  )
}

function PrebuiltToolCard({ tool }: { tool: Record<string, unknown> }) {
  const name = tool.name as string || ''
  const description = tool.description as string || ''
  const schema = tool.config_schema as Record<string, unknown> | undefined
  const configProps = (schema?.properties as Record<string, unknown>) ?? {}
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 hover:shadow-sm transition-all">
      <div className="flex items-start gap-3 mb-3">
        <div className="w-10 h-10 rounded-lg flex items-center justify-center text-base shrink-0"
          style={{ background: '#EFF6FF', color: '#2563EB' }}>
          <SafetyOutlined />
        </div>
        <div className="min-w-0">
          <div className="text-sm font-medium text-[#0F172A]">{name}</div>
          <div className="text-xs text-[#64748B] line-clamp-2">{description}</div>
        </div>
      </div>
      {Object.keys(configProps).length > 0 && (
        <div className="pt-3 border-t border-gray-50">
          <span className="text-[11px] text-[#94A3B8]">配置项:</span>
          <div className="flex flex-wrap gap-1.5 mt-1">
            {Object.keys(configProps).map(k => (
              <Tag key={k} className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded"
                style={{ color: '#2563EB', background: '#EFF6FF', borderColor: 'transparent' }}>{k}</Tag>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ===========================================================================
// Tab 2: Custom Tools (User-created CRUD)
// ===========================================================================

const SOURCE_TAGS: Record<string, { color: string; label: string; icon: typeof GlobalOutlined }> = {
  openapi: { color: 'blue', label: 'API', icon: GlobalOutlined },
  code: { color: 'green', label: 'Code', icon: CodeOutlined },
  prebuilt: { color: 'purple', label: 'Prebuilt', icon: ToolOutlined },
}

function CustomToolsTab() {
  const queryClient = useQueryClient()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [searchName, setSearchName] = useState('')

  const { data: allCustom, isLoading } = useQuery({
    queryKey: toolKeys.customTools(),
    queryFn: async () => {
      const [r1, r2] = await Promise.all([
        toolsApi.list({ page: 1, page_size: 100, source: 'openapi' }),
        toolsApi.list({ page: 1, page_size: 100, source: 'code' }),
      ])
      return [...r1.items, ...r2.items]
    },
  })

  const deleteMutation = useMutation({
    mutationFn: toolsApi.remove,
    onSuccess: () => {
      message.success('工具已删除')
      queryClient.invalidateQueries({ queryKey: toolKeys.customTools() })
      queryClient.invalidateQueries({ queryKey: toolKeys.lists() })
      queryClient.invalidateQueries({ queryKey: agentKeys.all })
    },
  })

  const handleDelete = (tool: Tool) => {
    Modal.confirm({
      title: '删除工具',
      content: `确定删除「${tool.name}」吗？引用此工具的 Agent 将失去该工具。`,
      okText: '删除', okType: 'danger', cancelText: '取消',
      onOk: () => deleteMutation.mutate(tool.id),
    })
  }

  const tools = (allCustom || []).filter(t => {
    if (!searchName) return true
    const q = searchName.toLowerCase()
    return t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="relative">
          <SearchOutlined className="absolute left-3 top-1/2 -translate-y-1/2 text-[#94A3B8] text-sm" />
          <input
            type="text" placeholder="搜索自定义工具..." value={searchName}
            onChange={(e) => setSearchName(e.target.value)}
            className="pl-9 pr-4 py-2 rounded-lg border border-gray-200 text-sm focus:outline-none focus:ring-2 w-64"
          />
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setDrawerOpen(true)}>
          创建工具
        </Button>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-20"><Spin size="large" /></div>
      ) : tools.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={<span>暂无自定义工具<br />点击「创建工具」添加 OpenAPI 或 Code 工具</span>}
          className="py-20"
        />
      ) : (
        <div className="grid grid-cols-3 gap-4">
          {tools.map(tool => <CustomToolCard key={tool.id} tool={tool} onDelete={() => handleDelete(tool)} />)}
        </div>
      )}

      <ToolCreateDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} />
    </div>
  )
}

function CustomToolCard({ tool, onDelete }: { tool: Tool; onDelete: () => void }) {
  const tag = SOURCE_TAGS[tool.source] || { color: 'default', label: tool.source, icon: ToolOutlined }
  const Icon = tag.icon
  const paramNames = Object.keys((tool.input_schema?.properties as Record<string, unknown>) ?? {})

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-5 hover:shadow-sm transition-all">
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-start gap-3 min-w-0">
          <div className="w-10 h-10 rounded-lg flex items-center justify-center text-base shrink-0"
            style={{ background: '#F1F5F9', color: '#6366F1' }}>
            <Icon />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-[#0F172A]">{tool.name}</span>
              <Tag color={tag.color} className="!m-0 !text-[10px]">{tag.label}</Tag>
            </div>
            <div className="text-xs text-[#64748B] line-clamp-2 mt-0.5">{tool.description}</div>
          </div>
        </div>
        <Button danger icon={<DeleteOutlined />} size="small" type="text" onClick={onDelete} />
      </div>
      {paramNames.length > 0 && (
        <div className="pt-3 border-t border-gray-50">
          <div className="flex flex-wrap gap-1.5">
            {paramNames.map(p => (
              <Tag key={p} className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded"
                style={{ color: '#64748B', background: '#F1F5F9', borderColor: 'transparent' }}>{p}</Tag>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

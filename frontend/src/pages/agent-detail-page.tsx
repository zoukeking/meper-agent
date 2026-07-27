/**
 * AgentDetailPage — Agent detail workspace with split layout.
 *
 * Route: /agents/:id  (id = real agent ID from API)
 *
 * Layout: left = configuration form, right = chat test panel.
 *
 * Provides an inline editing + testing experience so users can
 * tweak prompts and immediately test them side by side.
 */
import { useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Spin, message } from 'antd'
import { ArrowLeftOutlined, RobotOutlined, LockOutlined } from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import {
  agentApi,
  agentKeys,
} from '../services/agent-api'
import AgentConfigForm, { type AgentConfigFormHandle } from '../components/agent-config-form'
import ChatPanel from '../components/chat-panel'

/* ─── Component ─── */

export default function AgentDetailPage() {
  const { t } = useTheme()
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const formRef = useRef<AgentConfigFormHandle>(null)
  const [isSaving, setIsSaving] = useState(false)
  const queryClient = useQueryClient()

  /* ─── Query: agent detail ─── */
  const {
    data: agent,
    isLoading,
    isError,
  } = useQuery({
    queryKey: agentKeys.detail(id!),
    queryFn: () => agentApi.get(id!),
    enabled: !!id,
    refetchOnWindowFocus: false,
  })

  const isPublished = agent?.status === 'published'

  /* ─── Mutation: archive (unpublish) agent ─── */
  const archiveMutation = useMutation({
    mutationFn: agentApi.archive,
    onSuccess: () => {
      message.success('Agent 已下架为草稿，现在可以编辑了')
      queryClient.invalidateQueries({ queryKey: agentKeys.detail(id!) })
      queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
    },
  })

  /* ─── Loading / error states ─── */
  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Spin size="large" />
      </div>
    )
  }

  if (isError || !agent) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-[#94A3B8]">
        <RobotOutlined className="text-4xl mb-3" />
        <p className="text-sm mb-3">Agent 不存在或加载失败</p>
        <Button onClick={() => navigate('/agents')}>返回 Agent 列表</Button>
      </div>
    )
  }

  /* ─── Resolve model label for chat panel ─── */
  const agentModel = agent.default_model ?? ''

  return (
    <div className="animate-[fadeIn_0.3s_ease-out] flex gap-6 h-full">
      {/* ════════ Left: Configuration ════════ */}
      <div className="flex flex-col w-[480px] shrink-0 min-h-0">
        {/* Toolbar */}
        <div className="flex items-center justify-between mb-3 shrink-0">
          <button
            onClick={() => navigate('/agents')}
            className="flex items-center gap-1.5 border-0 bg-transparent text-sm text-[#64748B] hover:text-[#0F172A] transition-colors duration-150 px-0 cursor-pointer"
          >
            <ArrowLeftOutlined className="text-xs" />
            <span>返回列表</span>
          </button>
          <div className="flex items-center gap-2">
            <Button
              type="primary"
              onClick={() => formRef.current?.submit()}
              loading={isSaving}
              disabled={isPublished}
              style={{ background: t.primary, borderColor: t.primary }}
            >
              保存
            </Button>
          </div>
        </div>

        {/* Published immutability notice */}
        {isPublished && (
          <div className="mb-3 p-3 rounded-lg bg-amber-50 border border-amber-200 text-sm text-amber-800 flex items-center justify-between shrink-0">
            <span className="flex items-center gap-2">
              <LockOutlined />
              此 Agent 已发布，配置不可修改。如需修改请先下架。
            </span>
            <Button
              size="small"
              onClick={() => archiveMutation.mutate(agent.id)}
              loading={archiveMutation.isPending}
            >
              下架为草稿
            </Button>
          </div>
        )}

        {/* Form (scrollable) */}
        <div className="flex-1 overflow-y-auto rounded-xl border border-gray-200 bg-white p-5 min-h-0">
          <AgentConfigForm
            ref={formRef}
            agent={agent}
            mode="edit"
            onSavingChange={setIsSaving}
          />
        </div>
      </div>

      {/* ════════ Right: Chat test ════════ */}
      <div className="flex-1 min-w-0 min-h-0">
        <ChatPanel
          agentId={agent.id}
          agentName={agent.name}
          agentModel={agentModel}
          showSidebar={true}
          className="h-full"
        />
      </div>
    </div>
  )
}

/**
 * WorkflowProposalCard — 工作流提议确认卡片
 *
 * 当 LLM 调用 propose_workflow 后，后端返回 {type: "workflow_proposal", ...}
 * 前端检测到后渲染此卡片，让用户点击确认或拒绝。
 *
 * 用户点击 [确认执行] → 通过 onConfirm 回调发送用户消息 "确认执行"
 * 用户点击 [拒绝] → 卡片进入已拒绝状态
 */
import { useState } from 'react'
import { Button, Tag, Typography } from 'antd'
import {
  RobotOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  UserOutlined,
} from '@ant-design/icons'

const { Text, Paragraph } = Typography

export interface WorkflowProposal {
  type: 'workflow_proposal'
  workflow_name: string
  workflow_description: string
  input_preview: Record<string, unknown>
  /** Optional — only set by the legacy propose_workflow tool_result path;
   *  the confirm_workflow interrupt path does not carry this. */
  has_human_node?: boolean
}

interface WorkflowProposalCardProps {
  proposal: WorkflowProposal
  /** Called when the user clicks 确认执行.
   *  May return a Promise<boolean> that resolves to false when the send was
   *  suppressed (e.g. another stream is still running); the card then resets
   *  to idle instead of misleadingly showing "已确认". */
  onConfirm: (workflowName: string) => Promise<boolean> | boolean | void
  /** Force the card into a terminal state on mount — used for history
   *  backfill where the user has already confirmed/rejected (tool_result is
   *  present) and the card should be read-only. */
  forceAction?: 'confirmed' | 'rejected'
}

export default function WorkflowProposalCard({
  proposal,
  onConfirm,
  forceAction,
}: WorkflowProposalCardProps) {
  const [action, setAction] = useState<'idle' | 'confirming' | 'confirmed' | 'rejected'>(
    forceAction ?? 'idle',
  )
  // Set when the click was suppressed (e.g. clicked during streaming). Shown
  // inline so the user understands why nothing happened and can retry.
  const [suppressed, setSuppressed] = useState(false)

  const handleConfirm = async () => {
    setAction('confirming')
    setSuppressed(false)
    try {
      const result = await onConfirm(proposal.workflow_name)
      // Only flip to "confirmed" when the send actually went through.
      // A falsey result (suppressed by isStreaming guard) resets the card
      // so the user can retry instead of seeing a fake "已确认".
      if (result === false) {
        setAction('idle')
        setSuppressed(true)
      } else {
        setAction('confirmed')
      }
    } catch {
      setAction('idle')
    }
  }

  const handleReject = () => {
    setAction('rejected')
  }

  // Format input_preview for display
  const inputEntries = Object.entries(proposal.input_preview)

  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50/60 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-blue-100">
        <RobotOutlined className="text-blue-500 text-base" />
        <span className="text-sm font-semibold text-blue-700">工作流确认</span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-2">
        <div className="flex items-center gap-2">
          <Text type="secondary" className="text-xs">工作流:</Text>
          <Tag color="blue" className="text-xs">{proposal.workflow_name}</Tag>
        </div>

        {proposal.workflow_description && (
          <div>
            <Text type="secondary" className="text-xs">描述:</Text>
            <Paragraph className="text-xs text-gray-700 mb-0 mt-0.5">
              {proposal.workflow_description}
            </Paragraph>
          </div>
        )}

        {inputEntries.length > 0 && (
          <div>
            <Text type="secondary" className="text-xs">输入参数:</Text>
            <div className="mt-0.5 space-y-0.5">
              {inputEntries.map(([key, value]) => (
                <div key={key} className="flex items-start gap-2">
                  <Tag className="text-[10px]">{key}</Tag>
                  <Text className="text-xs text-gray-700">
                    {typeof value === 'string' ? value : JSON.stringify(value)}
                  </Text>
                </div>
              ))}
            </div>
          </div>
        )}

        {proposal.has_human_node && (
          <div className="flex items-center gap-1.5">
            <UserOutlined className="text-amber-500 text-xs" />
            <Text className="text-[10px] text-amber-600">该工作流包含人工审批节点</Text>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 px-4 py-3 border-t border-blue-100 bg-white/50">
        {action === 'idle' && (
          <>
            <Button
              type="primary"
              size="small"
              icon={<CheckCircleOutlined />}
              className="bg-blue-600 hover:bg-blue-700 border-blue-600 text-xs"
              onClick={handleConfirm}
            >
              确认执行
            </Button>
            <Button
              size="small"
              icon={<CloseCircleOutlined />}
              className="text-xs"
              danger
              onClick={handleReject}
            >
              拒绝
            </Button>
            {suppressed && (
              <Text className="text-[10px] text-amber-600 ml-1">
                Agent 正在响应，请稍后重试
              </Text>
            )}
          </>
        )}
        {action === 'confirming' && (
          <Text className="text-xs text-blue-600">正在发送确认...</Text>
        )}
        {action === 'confirmed' && (
          <Text className="text-xs text-green-600">
            <CheckCircleOutlined className="mr-1" />
            已确认
          </Text>
        )}
        {action === 'rejected' && (
          <Text className="text-xs text-gray-500">
            <CloseCircleOutlined className="mr-1" />
            已取消
          </Text>
        )}
      </div>
    </div>
  )
}

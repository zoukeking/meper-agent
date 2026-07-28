/**
 * WorkflowProposalCard — 工作流确认卡片（confirm_workflow 工具）
 *
 * 当 agent 调 confirm_workflow 后，后端 interrupt 挂起，前端从 tool_call 的
 * args（workflow_name / description / params）渲染此卡片，让用户确认或拒绝。
 *
 * 用户点 [确认执行] → onConfirm 回调发送 "确认执行 {name}"（走 /resume）
 * 用户点 [拒绝]     → 卡片进入已拒绝状态
 *
 * 历史回填时（已确认/已拒绝），forceAction 强制卡片进入对应终态（只读）。
 */
import { useState } from 'react';
import { Bot, CheckCircle2, XCircle } from 'lucide-react';

export interface WorkflowProposal {
  type: 'workflow_proposal';
  workflow_name: string;
  workflow_description: string;
  input_preview: Record<string, unknown>;
  /** Optional — confirm_workflow 不传此字段（旧 propose_workflow 路径才有）。 */
  has_human_node?: boolean;
}

interface WorkflowProposalCardProps {
  proposal: WorkflowProposal;
  /** Called when the user clicks 确认执行. */
  onConfirm: (workflowName: string) => Promise<boolean> | boolean | void;
  /** Force the card into a terminal state on mount — used for history backfill
   *  where the user has already confirmed/rejected (tool_result is present). */
  forceAction?: 'confirmed' | 'rejected';
}

export default function WorkflowProposalCard({
  proposal,
  onConfirm,
  forceAction,
}: WorkflowProposalCardProps) {
  const [action, setAction] = useState<'idle' | 'confirming' | 'confirmed' | 'rejected'>(
    forceAction ?? 'idle',
  );

  const handleConfirm = async () => {
    setAction('confirming');
    try {
      await onConfirm(proposal.workflow_name);
      setAction('confirmed');
    } catch {
      setAction('idle');
    }
  };

  const handleReject = () => {
    setAction('rejected');
  };

  const inputEntries = Object.entries(proposal.input_preview);

  return (
    <div className="rounded-xl rounded-tl-none border border-indigo-500/30 bg-indigo-500/10 overflow-hidden font-sans">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-indigo-500/20">
        <Bot className="text-indigo-300" size={16} />
        <span className="text-[13px] font-semibold text-indigo-200">工作流确认</span>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-2">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-zinc-400">工作流:</span>
          <span className="text-xs px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-200 border border-indigo-500/30">
            {proposal.workflow_name}
          </span>
        </div>

        {proposal.workflow_description && (
          <div>
            <span className="text-[11px] text-zinc-400">描述:</span>
            <p className="text-xs text-zinc-300 mt-0.5 leading-relaxed">
              {proposal.workflow_description}
            </p>
          </div>
        )}

        {inputEntries.length > 0 && (
          <div>
            <span className="text-[11px] text-zinc-400">输入参数:</span>
            <div className="mt-0.5 space-y-0.5">
              {inputEntries.map(([key, value]) => (
                <div key={key} className="flex items-start gap-2">
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 shrink-0">
                    {key}
                  </span>
                  <span className="text-xs text-zinc-300 break-all">
                    {typeof value === 'string' ? value : JSON.stringify(value)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {proposal.has_human_node && (
          <div className="text-[10px] text-amber-400">⚠ 该工作流包含人工审批节点</div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-t border-indigo-500/20 bg-[#121214]/40">
        {action === 'idle' && (
          <>
            <button
              type="button"
              onClick={handleConfirm}
              className="px-3.5 py-1.5 rounded-lg text-xs bg-indigo-600 text-white hover:bg-indigo-500 transition cursor-pointer inline-flex items-center gap-1.5"
            >
              <CheckCircle2 size={13} />
              确认执行
            </button>
            <button
              type="button"
              onClick={handleReject}
              className="px-3.5 py-1.5 rounded-lg text-xs bg-[#121214] border border-[#27272a] text-slate-300 hover:bg-[#27272a] transition cursor-pointer inline-flex items-center gap-1.5"
            >
              <XCircle size={13} />
              拒绝
            </button>
          </>
        )}
        {action === 'confirming' && (
          <span className="text-xs text-indigo-300">正在发送确认...</span>
        )}
        {action === 'confirmed' && (
          <span className="text-xs text-emerald-400 inline-flex items-center gap-1">
            <CheckCircle2 size={13} />
            已确认
          </span>
        )}
        {action === 'rejected' && (
          <span className="text-xs text-zinc-500 inline-flex items-center gap-1">
            <XCircle size={13} />
            已取消
          </span>
        )}
      </div>
    </div>
  );
}

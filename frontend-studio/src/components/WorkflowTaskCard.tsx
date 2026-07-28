/**
 * WorkflowTaskCard — dispatch_workflow 工具结果的内嵌可展开卡片。
 *
 * dispatch_workflow 触发工作流后返回 {type:'task_created', task_id, workflow_id, ...}。
 * 本卡片只读 tool result 里的 task_id，自行 fetch 任务详情、轮询状态、发干预请求，
 * 不修改 chat history（消息记录里的 tool result 字符串原样保留）；刷新页面/切会话
 * 回来后卡片重新挂载，仍能拉到最新 task 状态。
 *
 * 复用：tasksApi / taskKeys / TASK_STATUS_STYLES / TaskOutputFiles（产物文件）。
 * 实时：react-query useQuery，运行态按 POLL_MS 轮询、终态停止；操作走 useMutation
 * 调 /tasks/{id}/intervene（带 version 乐观锁，409 自动刷新）。
 * 降级：fetch 失败/无权限 → 只展示首屏 task_created 摘要 + 错误提示，不阻断对话。
 */
import { useState, type ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Check, X, Ban, RotateCcw, Play, SkipForward, Copy, Loader2,
  ChevronRight, AlertTriangle, RefreshCw,
} from 'lucide-react';
import {
  tasksApi, taskKeys,
  type TaskDetail, type TaskStatusValue, type CommentValue,
} from '../services/tasks-api';
import { TASK_STATUS_STYLES } from '../constants/task-status';
import { TaskOutputFiles } from './task/TaskOutputFiles';
import { TaskFlowTimeline } from './task/TaskFlowTimeline';
import { toast } from './ui/toast';

/** 轮询间隔：可经 VITE_TASK_POLL_INTERVAL_MS 配置，默认 15s。 */
const POLL_MS = Number(import.meta.env.VITE_TASK_POLL_INTERVAL_MS) || 15000;

const RUNNING_STATUSES: TaskStatusValue[] = ['pending', 'running', 'waiting_human'];

/** dispatch_workflow 返回的 task_created 结构（首屏即时数据，不等待 fetch）。 */
export interface TaskCreated {
  type: 'task_created';
  task_id: string;
  workflow_id?: string;
  workflow_name?: string;
  workflow_description?: string;
  status?: TaskStatusValue;
  has_human_node?: boolean;
  message?: string;
}

/** 解析 tool result 文本为 TaskCreated；非 task_created 结构返回 null（调用方降级到通用工具卡）。 */
export function parseTaskCreated(raw?: string): TaskCreated | null {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!trimmed.startsWith('{')) return null;
  try {
    const obj = JSON.parse(trimmed) as Record<string, unknown>;
    if (obj?.type === 'task_created' && typeof obj.task_id === 'string') {
      return obj as unknown as TaskCreated;
    }
  } catch {
    /* ignore */
  }
  return null;
}

const ACTION_LABEL: Record<string, string> = {
  approve: '已通过',
  reject: '已驳回',
  skip: '已跳过',
  cancel: '已取消',
  retry: '已重新执行',
  resume: '已继续',
};

function JsonBlock({ value }: { value: unknown }) {
  let text: string;
  try {
    text =
      typeof value === 'string'
        ? JSON.stringify(JSON.parse(value), null, 2)
        : JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  if (!text || text === '{}' || text === 'null') return null;
  return (
    <pre className="text-[11px] leading-relaxed whitespace-pre-wrap break-all rounded-lg p-2 bg-[#121214] border border-[#27272a] text-[#a1a1aa] font-mono max-h-56 overflow-y-auto">
      {text}
    </pre>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[10px] font-semibold mb-1 text-[#a1a1aa] opacity-60">{title}</div>
      {children}
    </div>
  );
}

export function WorkflowTaskCard({ created }: { created: TaskCreated }) {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(true);
  const [pendingAction, setPendingAction] = useState<null | 'reject' | 'cancel'>(null);
  const [comment, setComment] = useState('');

  const detailQ = useQuery({
    queryKey: taskKeys.detail(created.task_id),
    queryFn: () => tasksApi.get(created.task_id),
    // 首次未加载完之前默认轮询；拿到 task 后按其状态决定（运行态轮询、终态停止）。
    refetchInterval: (query) => {
      const data = query.state.data as TaskDetail | undefined;
      return !data || RUNNING_STATUSES.includes(data.status) ? POLL_MS : false;
    },
    retry: false,
  });
  const task = detailQ.data;
  const fetchError = detailQ.error as { statusCode?: number; message?: string } | null;

  const status: TaskStatusValue = task?.status ?? created.status ?? 'pending';
  const style = TASK_STATUS_STYLES[status];
  const isRunning = RUNNING_STATUSES.includes(status);

  const interveneMut = useMutation({
    mutationFn: (vars: { action: string; comment?: CommentValue }) =>
      tasksApi.intervene(created.task_id, {
        action: vars.action,
        comment: vars.comment,
        version: task!.version,
      }),
    onSuccess: (_d, vars) => {
      toast.success(ACTION_LABEL[vars.action] ?? '操作成功');
      setPendingAction(null);
      setComment('');
      qc.invalidateQueries({ queryKey: taskKeys.detail(created.task_id) });
    },
    onError: (err) => {
      const e = err as { statusCode?: number; message?: string };
      if (e.statusCode === 409) {
        // 409 冲突：刷新详情（错误提示由全局 mutations.onError 兜底）
        qc.invalidateQueries({ queryKey: taskKeys.detail(created.task_id) });
      }
    },
  });

  const doAction = (action: 'approve' | 'skip' | 'retry' | 'resume') => {
    if (!task) return;
    interveneMut.mutate({ action });
  };

  const submitPending = () => {
    if (!task || !pendingAction) return;
    const text = comment.trim();
    interveneMut.mutate({
      action: pendingAction === 'reject' ? 'reject' : 'cancel',
      comment: text ? { type: 'text', value: text } : undefined,
    });
  };

  const copyId = () => {
    void navigator.clipboard.writeText(created.task_id).then(() => toast.success('已复制 Task ID'));
  };

  const ckpt = task?.checkpoint ?? null;

  return (
    <div className="rounded-xl rounded-tl-none border border-[#27272a] bg-[#18181b] overflow-hidden font-sans">
      {/* Header — click to toggle */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3.5 py-2.5 bg-transparent border-0 text-left cursor-pointer hover:bg-[#1c1c1f] transition-colors"
      >
        <span
          className={`w-2.5 h-2.5 rounded-full shrink-0 ${style.pulse ? 'animate-pulse' : ''}`}
          style={{ backgroundColor: style.color }}
        />
        <span className="text-xs font-semibold text-[#fafafa] truncate">
          {created.workflow_name || '工作流任务'}
        </span>
        <span
          className="px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0"
          style={{ backgroundColor: style.bg, color: style.color }}
        >
          {style.label}
        </span>
        <span className="text-[10px] text-[#71717a] font-mono truncate ml-1">{created.task_id}</span>
        <span
          role="button"
          onClick={(e) => {
            e.stopPropagation();
            copyId();
          }}
          className="ml-auto shrink-0 text-[#71717a] hover:text-[#fafafa] transition-colors cursor-pointer"
          title="复制 Task ID"
        >
          <Copy className="w-3.5 h-3.5" />
        </span>
        <ChevronRight className={`w-3.5 h-3.5 text-[#71717a] transition-transform ${expanded ? 'rotate-90' : ''}`} />
      </button>

      {/* Body */}
      {expanded && (
        <div className="border-t border-[#27272a] px-3.5 pb-3 pt-2.5 space-y-2.5">
          {detailQ.isLoading && !task ? (
            <div className="flex items-center gap-1.5 text-[11px] text-[#71717a]">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> 加载任务详情…
            </div>
          ) : null}

          {fetchError ? (
            <div className="text-[11px] text-amber-300 flex items-center gap-1.5">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
              <span className="flex-1">无法加载任务详情{fetchError.message ? `：${fetchError.message}` : ''}</span>
              <button
                type="button"
                onClick={() => detailQ.refetch()}
                className="text-amber-300/80 hover:text-amber-200 underline"
              >
                重试
              </button>
            </div>
          ) : null}

          {/* 人工节点上下文（waiting_human 时置顶） */}
          {ckpt && status === 'waiting_human' ? (
            <div className="rounded-lg border border-[#8B5CF6]/30 bg-[#8B5CF6]/10 p-2.5 space-y-1">
              <div className="text-[11px] font-semibold text-[#c4b5fd]">
                {ckpt.human_context?.title || '需要人工确认'}
              </div>
              {ckpt.human_context?.description ? (
                <div className="text-[11px] text-[#ddd6fe] whitespace-pre-wrap">{ckpt.human_context.description}</div>
              ) : null}
              {ckpt.human_context?.options?.length ? (
                <div className="flex flex-wrap gap-1.5 pt-1">
                  {ckpt.human_context.options.map((o) => (
                    <span key={o} className="px-1.5 py-0.5 rounded bg-[#121214] border border-[#27272a] text-[10px] text-[#a1a1aa]">
                      {o}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}

          {task?.error ? (
            <Section title="错误信息">
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-[11px] text-red-300 space-y-0.5">
                {task.error.error_code ? <div className="font-mono">[{task.error.error_code}]</div> : null}
                <div className="whitespace-pre-wrap">{task.error.error_message}</div>
                {task.error.node_id ? <div className="text-[10px] opacity-70">节点：{task.error.node_id}</div> : null}
              </div>
            </Section>
          ) : null}

          {task?.input && Object.keys(task.input).length ? (
            <Section title="输入参数">
              <JsonBlock value={task.input} />
            </Section>
          ) : null}

          {task?.output ? (
            <Section title="输出结果">
              <JsonBlock value={task.output} />
            </Section>
          ) : null}

          {task ? (
            <div>
              <div className="text-[10px] font-semibold mb-1 text-[#a1a1aa] opacity-60">执行轨迹</div>
              {/* 复用任务详情抽屉的流程时间线：fetch workflow 建节点名映射、
                  按 node_id 分组渲染（节点名+类型图标+状态+耗时）、agent 节点
                  支持「查看执行详情」、首尾带任务里程碑。 */}
              <TaskFlowTimeline task={task} theme="dark" />
            </div>
          ) : null}

          {status === 'completed' || status === 'running' ? <TaskOutputFiles taskId={created.task_id} /> : null}

          {/* 操作区（按状态） */}
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            {status === 'waiting_human' ? (
              <>
                <ActBtn icon={Check} label="通过" tone="ok" loading={interveneMut.isPending} onClick={() => doAction('approve')} />
                <ActBtn icon={X} label="驳回" tone="warn" loading={interveneMut.isPending} onClick={() => setPendingAction('reject')} />
                <ActBtn icon={SkipForward} label="跳过" tone="neutral" loading={interveneMut.isPending} onClick={() => doAction('skip')} />
              </>
            ) : null}
            {status === 'running' ? (
              <ActBtn icon={Ban} label="取消" tone="warn" loading={interveneMut.isPending} onClick={() => setPendingAction('cancel')} />
            ) : null}
            {status === 'failed' ? (
              <ActBtn icon={RotateCcw} label="重试" tone="ok" loading={interveneMut.isPending} onClick={() => doAction('retry')} />
            ) : null}
            {status === 'cancelled' ? (
              <ActBtn icon={Play} label="继续" tone="ok" loading={interveneMut.isPending} onClick={() => doAction('resume')} />
            ) : null}
            {!isRunning && task ? (
              <button
                type="button"
                onClick={() => detailQ.refetch()}
                className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-[#71717a] hover:text-[#fafafa] hover:bg-[#27272a] transition-colors cursor-pointer"
                title="刷新"
              >
                <RefreshCw className="w-3 h-3" /> 刷新
              </button>
            ) : null}
          </div>

          {/* 驳回/取消的 comment 输入 */}
          {pendingAction ? (
            <div className="rounded-lg border border-[#27272a] bg-[#121214] p-2 space-y-1.5">
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                placeholder={pendingAction === 'reject' ? '驳回理由（可选）' : '取消原因（可选）'}
                rows={2}
                className="w-full bg-[#09090b] border border-[#27272a] rounded p-1.5 text-[11px] text-[#fafafa] resize-none outline-none focus:border-indigo-500/50"
              />
              <div className="flex gap-1.5 justify-end">
                <button
                  type="button"
                  onClick={() => {
                    setPendingAction(null);
                    setComment('');
                  }}
                  className="px-2 py-1 rounded text-[11px] text-[#a1a1aa] hover:bg-[#27272a] cursor-pointer"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={submitPending}
                  disabled={interveneMut.isPending}
                  className="px-2 py-1 rounded text-[11px] bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 hover:bg-indigo-500/30 disabled:opacity-50 cursor-pointer"
                >
                  确认{pendingAction === 'reject' ? '驳回' : '取消'}
                </button>
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function ActBtn({
  icon: Icon,
  label,
  tone,
  loading,
  onClick,
}: {
  icon: typeof Check;
  label: string;
  tone: 'ok' | 'warn' | 'neutral';
  loading?: boolean;
  onClick: () => void;
}) {
  const cls =
    tone === 'ok'
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20'
      : tone === 'warn'
        ? 'border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20'
        : 'border-[#3f3f46] bg-[#27272a]/40 text-[#d4d4d8] hover:bg-[#27272a]';
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={loading}
      className={`flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] border transition-colors disabled:opacity-50 cursor-pointer ${cls}`}
    >
      {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : <Icon className="w-3 h-3" />}
      {label}
    </button>
  );
}

import { useState, useRef, useEffect, FormEvent, useCallback, type MouseEvent, type ChangeEvent } from 'react';
import { Agent, Message, type ChatAttachment, type TimelineEntry } from '../types';
import {
  Send, Plus, ChevronDown, Sparkles, Trash2, FileCode, CheckCircle,
  Bot, Terminal, Loader2, Paperclip, Brain, X,
  Wrench, AlertTriangle, ChevronRight, User, Download, FileText, Image as ImageIcon,
} from 'lucide-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  sessionApi, sessionKeys, type Session, type MessageRecord, type FileRef, getFileId,
} from '../services/session-api';
import { agentApi, agentKeys } from '../services/agent-api';
import { modelApi, modelKeys } from '../services/model-api';
import { toStudioAgent } from '../services/adapters';
import { getFileBlob, downloadFile as downloadFileById } from '../services/file-api';
import { parseSSEStream } from '../lib/sse-parser';
import { SessionFilesPanel, type SessionFilesPanelHandle } from './SessionFilesPanel';
import { Markdown } from './Markdown';
import { detectPreviewKind } from './FilePreview';
import { FilePreviewModal } from './FilePreviewModal';
import { WorkflowTaskCard, parseTaskCreated } from './WorkflowTaskCard';
import WorkflowProposalCard from './workflow-proposal-card';
import { ClarificationFormCard, type ClarificationField } from './clarification-form-card';

interface ChatHomepageProps {
  /** Studio agents already adapted to the view model; if absent we fetch. */
  agents?: Agent[];
  theme?: 'light' | 'dark';
}

/** Convert a stored agent message + its timeline into display Messages. */
/**
 * 从工具结果文本里解析 agent 写出的 output 文件路径。
 * write_to_output 返回形如 "Successfully wrote 123 bytes to output/report.png"，
 * 路径相对 output/，即 sessionApi.previewFile 的 filePath 入参，无需二次转换。
 * 也可命中 "output/xxx" 字面量（agent 在 markdown 链接里引用产物时）。
 * 返回去重后的 ChatAttachment[]（source='output'）。
 */
const OUTPUT_PATH_RE = /\boutput\/([^\s"'<>)\\]+\.[A-Za-z0-9]+)/g;
export function parseOutputAttachments(text: string): ChatAttachment[] {
  if (!text) return [];
  const seen = new Set<string>();
  const out: ChatAttachment[] = [];
  for (const match of text.matchAll(OUTPUT_PATH_RE)) {
    const rel = match[1]; // 相对 output/ 的路径
    if (seen.has(rel)) continue;
    seen.add(rel);
    out.push({
      source: 'output',
      ref: rel,
      name: rel.split('/').pop() || rel,
    });
  }
  return out;
}

/** Convert a persisted backend agent MessageRecord into a single studio
 *  Message whose `timeline` carries text/tool/thinking/error entries in
 *  chronological order (mirrors frontend historyEntryToTimeline). Tool_call
 *  + tool_result are merged into one tool entry; an unanswered
 *  ask_clarification/confirm_workflow tool_call stays interactive
 *  (isInterrupted). Output-file attachments parsed from tool results are
 *  attached to the message. */
function agentMessageToDisplay(rec: MessageRecord, agentName: string, avatar: string): Message {
  const timeline: TimelineEntry[] = [];
  // Map tool_name → index of the last pending tool_call entry (for merging
  // the matching tool_result).
  const pendingToolCalls = new Map<string, number>();
  const outputAtts: ChatAttachment[] = [];
  let isInterrupted = false;

  for (let i = 0; i < (rec.timeline_entries ?? []).length; i++) {
    const entry = (rec.timeline_entries ?? [])[i];
    if (entry.type === 'text' || entry.type === 'final_answer') {
      const t = (entry.content ?? '').trim();
      if (t) timeline.push({ id: `${rec._id}-text-${i}`, type: 'text', content: t });
    } else if (entry.type === 'thinking') {
      timeline.push({ id: `${rec._id}-think-${i}`, type: 'thinking', content: entry.content ?? '' });
    } else if ((entry.type as string) === 'error') {
      timeline.push({ id: `${rec._id}-err-${i}`, type: 'error', content: `❌ ${entry.content || '执行出错'}` });
    } else if (entry.type === 'tool_call' || entry.type === 'tool') {
      const name = entry.tool_name ?? '';
      // tool entry (already merged by backend) or tool_call (need merge).
      const isError =
        entry.type === 'tool' &&
        typeof entry.content === 'string' &&
        /\b(error|fail)/i.test(entry.content);
      const isInterruptTool = name === 'ask_clarification' || name === 'confirm_workflow';
      const idx = timeline.push({
        id: `${rec._id}-tool-${name}-${i}`,
        type: 'tool',
        content: '',
        toolName: name,
        args: entry.args,
        toolStatus: isInterruptTool
          ? 'success'
          : entry.type === 'tool'
            ? (isError ? 'error' : 'success')
            : 'running',
        result: entry.type === 'tool' ? entry.content : undefined,
      }) - 1;
      if (entry.type === 'tool_call' && name) pendingToolCalls.set(name, idx);
      // 收集 output 产物。
      if (entry.type === 'tool' && typeof entry.content === 'string') {
        outputAtts.push(...parseOutputAttachments(entry.content));
      }
    } else if (entry.type === 'tool_result') {
      const name = entry.tool_name ?? '';
      // 优先用后端的 status 字段判错，回退正则。
      const isError =
        entry.status === 'error' ||
        (typeof entry.content === 'string' && /\b(error|fail)/i.test(entry.content));
      const pendingIdx = name ? pendingToolCalls.get(name) : undefined;
      if (pendingIdx !== undefined && timeline[pendingIdx]) {
        timeline[pendingIdx] = {
          ...timeline[pendingIdx],
          result: entry.content ?? '',
          toolStatus: isError ? 'error' : 'success',
        };
        pendingToolCalls.delete(name);
      } else {
        timeline.push({
          id: `${rec._id}-tool-${name}-${i}`,
          type: 'tool',
          content: '',
          toolName: name,
          result: entry.content ?? '',
          toolStatus: isError ? 'error' : 'success',
        });
      }
      if (typeof entry.content === 'string') {
        outputAtts.push(...parseOutputAttachments(entry.content));
      }
    }
    // tool_call_start / interrupt are transient — skip in history.
  }

  // 未答的 ask_clarification/confirm_workflow（tool_call 无 tool_result）→ 中断态。
  for (const e of timeline) {
    if (
      e.type === 'tool' &&
      (e.toolName === 'ask_clarification' || e.toolName === 'confirm_workflow') &&
      !e.result
    ) {
      isInterrupted = true;
      break;
    }
  }

  const dedupOutput = outputAtts.filter(
    (a, i, arr) => arr.findIndex((b) => b.ref === a.ref) === i,
  );

  return {
    id: rec._id,
    senderName: agentName,
    avatar,
    role: 'agent',
    content: '',
    timestamp: new Date(rec.created_at).toLocaleString(),
    timeline: timeline.length > 0 ? timeline : undefined,
    isInterrupted,
    attachment: fileRefToAttachment(rec.files?.[0]),
    attachments: dedupOutput.length > 0 ? dedupOutput : undefined,
    usage: rec.token_usage,
  };
}

function fileRefToAttachment(file?: FileRef): Message['attachment'] | undefined {
  if (!file) return undefined;
  const name = file.name;
  const mime = file.mime_type ?? '';
  const type: Message['attachment']['type'] = mime.startsWith('image/')
    ? 'image'
    : mime.startsWith('video/')
      ? 'video'
      : mime.includes('markdown') || name.endsWith('.md')
        ? 'markdown'
        : 'code';
  return { name, type, content: file.storage_key || name };
}

/** 机器人头像：默认 emoji 用 AFLogo.png，自定义 emoji 原样展示（后续再优化）。
 *  `DEFAULT_AVATAR_EMOJI` 与 services/adapters.ts 的 DEFAULT_AGENT_AVATAR 保持一致。 */
const DEFAULT_AVATAR_EMOJI = '🤖';
function BotAvatar({ avatar, className }: { avatar?: string; className?: string }) {
  if (!avatar || avatar === DEFAULT_AVATAR_EMOJI) {
    return (
      <img
        src="/AFLogo.png"
        alt="bot"
        className={`object-contain select-none ${className ?? ''}`}
        draggable={false}
      />
    );
  }
  return <span className={`leading-none ${className ?? ''}`}>{avatar}</span>;
}

/* ─── 对话内附件预览（用户上传 + agent 产出统一渲染） ─── */

/** 按 source 取 blob：upload 走 FileRef.id，output 走 session output 路径。 */
async function fetchAttachmentBlob(att: ChatAttachment, sessionId: string | null): Promise<Blob> {
  if (att.source === 'upload') {
    return getFileBlob(att.ref);
  }
  if (!sessionId) throw new Error('会话未加载，无法预览产出文件');
  const { blob } = await sessionApi.previewFile(sessionId, att.ref);
  return blob;
}

/** 下载附件：upload 用 file-api（<a download>），output 用 sessionApi.downloadFile。 */
async function downloadAttachment(att: ChatAttachment, sessionId: string | null): Promise<void> {
  if (att.source === 'upload') {
    await downloadFileById(att.ref, att.name);
    return;
  }
  if (!sessionId) throw new Error('会话未加载，无法下载产出文件');
  await sessionApi.downloadFile(sessionId, att.ref);
}

/** 字节数格式化（与 SessionFilesPanel 的 formatFileSize 一致）。 */
function formatBytes(bytes?: number): string {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const val = bytes / Math.pow(1024, i);
  return `${val < 10 ? val.toFixed(1) : Math.round(val)} ${units[i]}`;
}

/**
 * 单个附件的内联展示：图片懒加载缩略图直显，其余类型显示文件卡片。
 * 点击图片或「预览」打开 FilePreviewModal（复用，支持缩放/Esc/富渲染/下载）。
 * 需要父级传入 sessionId（output 来源取数依赖）。
 */
function ChatAttachmentCard({
  att,
  sessionId,
}: {
  att: ChatAttachment;
  sessionId: string | null;
}) {
  const kind = detectPreviewKind(att.name, att.mime_type);
  const isImage = kind === 'image';

  // 图片缩略图 blob（懒加载，卸载时回收 objectURL）
  const [thumbUrl, setThumbUrl] = useState<string>();
  const [thumbError, setThumbError] = useState(false);
  const [loadingThumb, setLoadingThumb] = useState(isImage);

  // 弹窗预览态：打开时按需拉取并按类型喂给 FilePreviewModal
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewText, setPreviewText] = useState<string>();
  const [previewImageUrl, setPreviewImageUrl] = useState<string>();

  // 图片缩略图：挂载即拉一次（output 图片同样从 blob 取）
  useEffect(() => {
    if (!isImage) return;
    let url: string | undefined;
    let cancelled = false;
    (async () => {
      try {
        const blob = await fetchAttachmentBlob(att, sessionId);
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setThumbUrl(url);
      } catch {
        if (!cancelled) setThumbError(true);
      } finally {
        if (!cancelled) setLoadingThumb(false);
      }
    })();
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [att, sessionId, isImage]);

  // 打开弹窗预览：按 detectPreviewKind 决定喂 text 还是 imageUrl；二进制直接走下载
  const openPreview = async () => {
    if (kind === 'binary') {
      // 不可预览：直接下载
      try {
        await downloadAttachment(att, sessionId);
      } catch {
        /* ignore */
      }
      return;
    }
    setPreviewOpen(true);
    setPreviewLoading(true);
    try {
      const blob = await fetchAttachmentBlob(att, sessionId);
      if (kind === 'image') {
        setPreviewImageUrl(URL.createObjectURL(blob));
        setPreviewText(undefined);
      } else {
        setPreviewText(await blob.text());
        setPreviewImageUrl(undefined);
      }
    } catch {
      // 拉取失败：留空，FilePreview 会渲染占位
    } finally {
      setPreviewLoading(false);
    }
  };

  // 关闭弹窗时回收 imageUrl（text 不需要回收）
  const closePreview = () => {
    if (previewImageUrl) URL.revokeObjectURL(previewImageUrl);
    setPreviewImageUrl(undefined);
    setPreviewText(undefined);
    setPreviewOpen(false);
  };

  // ── 图片：缩略图直显（可点击放大） ──
  if (isImage) {
    return (
      <>
        <button
          type="button"
          onClick={openPreview}
          className="relative rounded-lg overflow-hidden border border-[#27272a] bg-[#121214] hover:border-indigo-500/50 transition-colors cursor-pointer block"
          title={att.name}
        >
          {loadingThumb ? (
            <div className="w-48 h-32 flex items-center justify-center text-[#71717a]">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          ) : thumbError ? (
            <div className="w-48 h-32 flex flex-col items-center justify-center text-[#71717a] gap-1 px-2">
              <ImageIcon className="w-5 h-5" />
              <span className="text-[10px] truncate max-w-full">{att.name}</span>
            </div>
          ) : (
            thumbUrl && (
              <img
                src={thumbUrl}
                alt={att.name}
                className="max-h-48 max-w-full object-contain block"
                draggable={false}
              />
            )
          )}
        </button>
        <FilePreviewModal
          open={previewOpen}
          filename={att.name}
          mime={att.mime_type}
          imageUrl={previewImageUrl}
          text={previewText}
          onClose={closePreview}
          onDownload={() => downloadAttachment(att, sessionId)}
        />
      </>
    );
  }

  // ── 非图片：文件卡片（名称 + 类型徽标 + 预览/下载） ──
  return (
    <>
      <div className="flex items-center gap-2.5 w-72 px-3 py-2.5 rounded-lg border border-[#27272a] bg-[#18181b] hover:border-indigo-500/40 transition-colors group">
        <div className="w-8 h-8 rounded-md bg-[#121214] border border-[#27272a] flex items-center justify-center shrink-0">
          <FileText className="w-4 h-4 text-indigo-400" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-semibold text-slate-200 truncate" title={att.name}>
            {att.name}
          </p>
          <p className="text-[9px] text-[#71717a] font-mono uppercase">
            {att.mime_type || kind}
            {att.size ? ` · ${formatBytes(att.size)}` : ''}
          </p>
        </div>
        {kind === 'binary' ? (
          <button
            type="button"
            onClick={() => downloadAttachment(att, sessionId)}
            title="下载"
            className="p-1.5 text-[#71717a] hover:text-indigo-400 cursor-pointer transition-colors shrink-0"
          >
            <Download className="w-3.5 h-3.5" />
          </button>
        ) : (
          <button
            type="button"
            onClick={openPreview}
            title="预览"
            className="p-1.5 text-[#71717a] hover:text-indigo-400 cursor-pointer transition-colors shrink-0"
          >
            <FileCode className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
      <FilePreviewModal
        open={previewOpen}
        filename={att.name}
        mime={att.mime_type}
        text={previewText}
        imageUrl={previewImageUrl}
        onClose={closePreview}
        onDownload={() => downloadAttachment(att, sessionId)}
      />
      {previewLoading && previewOpen && null}
    </>
  );
}

function userMessageToDisplay(rec: MessageRecord): Message {
  // 全部附件（非仅首个）映射成可预览 ChatAttachment；source='upload' 走 getFileBlob(id)
  const attachments: ChatAttachment[] | undefined =
    rec.files && rec.files.length > 0
      ? rec.files.map((f) => ({
          source: 'upload' as const,
          ref: getFileId(f),
          name: f.name,
          mime_type: f.mime_type,
          size: f.size,
        }))
      : undefined;
  return {
    id: rec._id,
    senderName: '我',
    avatar: '👩‍💼',
    role: 'user',
    content: rec.content,
    timestamp: new Date(rec.created_at).toLocaleString(),
    attachment: fileRefToAttachment(rec.files?.[0]),
    attachments,
  };
}

export function ChatHomepage({ agents: agentsProp, theme = 'dark' }: ChatHomepageProps) {
  const qc = useQueryClient();

  // ── Agents (fallback fetch if not passed in) ──
  const { data: agentsData } = useQuery({
    queryKey: agentKeys.list({ page: 1, page_size: 50, status: 'all' }),
    queryFn: () => agentApi.list({ page: 1, page_size: 50, status: 'all' }),
    enabled: !agentsProp || agentsProp.length === 0,
    staleTime: 60_000,
  });
  const agents: Agent[] =
    agentsProp && agentsProp.length > 0
      ? agentsProp
      : (agentsData?.items ?? []).map(toStudioAgent);

  // ── Models: build an id → name map so the chat header / input box can show
  // the human-readable model name instead of the raw "model_..." id. ──
  const { data: modelsData } = useQuery({
    queryKey: modelKeys.list({ page_size: 100 }),
    queryFn: () => modelApi.list({ page_size: 100 }),
    staleTime: 60_000,
  });
  const modelNameById = new Map(
    (modelsData?.items ?? []).map((m) => [m.id, m.name]),
  );
  const modelLabel = (id?: string) => (id ? (modelNameById.get(id) ?? id) : 'Auto');

  // ── Sessions list ──
  const { data: sessionsData, isLoading: sessionsLoading } = useQuery({
    queryKey: sessionKeys.lists(),
    queryFn: () => sessionApi.list({ page: 1, page_size: 50 }),
    staleTime: 15_000,
  });
  const sessions: Session[] = sessionsData?.items ?? [];

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [showDropdown, setShowDropdown] = useState(false);
  const [showAgentSelectModal, setShowAgentSelectModal] = useState(false);
  const [inputText, setInputText] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  // Live messages for the active session (history + in-flight stream deltas).
  const [liveMessages, setLiveMessages] = useState<Message[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  // Thinking mode toggle (enable_thinking execution param) + pending file attachments.
  // NOTE: pendingFiles holds raw File objects queued locally — the actual upload
  // happens inside handleSendMessage (before the stream fires), mirroring
  // frontend/src/components/chat-panel.tsx. This avoids the race where the text
  // prompt is sent before the file upload resolves.
  const [enableThinking, setEnableThinking] = useState(true);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  // Right-pane tab: 'chat' (messages) | 'files' (generated-file manager).
  const [rightTab, setRightTab] = useState<'chat' | 'files'>('chat');

  const messageEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const filesPanelRef = useRef<SessionFilesPanelHandle>(null);
  // ask_clarification / confirm_workflow 中断态：SSE 收到 interrupt 或历史回填时
  // 记录对应的消息 id，使下一次发送走 /resume 而非 /stream（避免 agent 重复提问）。
  const pendingInterruptRef = useRef<{ toolMsgId: string } | null>(null);

  // ── Timeline 流式渲染 refs（对齐 frontend chat-panel 的 RAF 批处理）──
  // 每个 text_delta 只进 buffer，一帧最多 setState 一次，保证平滑且多轮
  // text→tool→text 顺序正确（tool_call_start 重置 textEntryIdRef 让新轮 text
  // 开新 entry）。
  const deltaBufferRef = useRef<{ agentMsgId: string; delta: string } | null>(null);
  const rafIdRef = useRef<number | null>(null);
  const textEntryIdRef = useRef<string | null>(null);
  const textStartedRef = useRef(false);

  /** 把累积的 delta flush 进 agent 消息的 timeline（追加到当前 text entry 或新建）。 */
  const flushDelta = useCallback(() => {
    rafIdRef.current = null;
    const buf = deltaBufferRef.current;
    if (!buf) return;
    deltaBufferRef.current = null;
    const { agentMsgId, delta } = buf;
    setLiveMessages((prev) =>
      prev.map((m) => {
        if (m.id !== agentMsgId) return m;
        const tl = [...(m.timeline ?? [])];
        const existingId = textEntryIdRef.current;
        if (
          existingId &&
          tl.length > 0 &&
          tl[tl.length - 1].id === existingId &&
          tl[tl.length - 1].type === 'text'
        ) {
          // 快路径：ref 命中末尾 text entry，直接追加。
          tl[tl.length - 1] = { ...tl[tl.length - 1], content: tl[tl.length - 1].content + delta };
        } else if (textStartedRef.current && tl.length > 0 && tl[tl.length - 1].type === 'text') {
          // 同一 LLM 输出阶段（无 tool_call 介入），合并进末尾 text entry。
          const lastIdx = tl.length - 1;
          tl[lastIdx] = { ...tl[lastIdx], content: tl[lastIdx].content + delta };
          textEntryIdRef.current = tl[lastIdx].id;
        } else {
          // 新文本块（首个 delta，或 tool_call 之后）：push 新 entry。
          const newId = `${agentMsgId}-text-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
          tl.push({ id: newId, type: 'text', content: delta });
          textEntryIdRef.current = newId;
          textStartedRef.current = true;
        }
        return { ...m, timeline: tl };
      }),
    );
    messageEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  /** 累积 text_delta 进 buffer，调度 RAF flush（一帧一次）。 */
  const appendDelta = useCallback(
    (agentMsgId: string, delta: string) => {
      const prev = deltaBufferRef.current;
      if (prev && prev.agentMsgId === agentMsgId) prev.delta += delta;
      else deltaBufferRef.current = { agentMsgId, delta };
      if (!rafIdRef.current) rafIdRef.current = requestAnimationFrame(flushDelta);
    },
    [flushDelta],
  );

  const activeSession = sessions.find((s) => s._id === activeSessionId) ?? null;
  const activeAgent =
    agents.find((a) => a.id === activeSession?.agent_id) ?? agents[0] ?? null;

  // Auto-select first session once loaded.
  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      setActiveSessionId(sessions[0]._id);
    }
  }, [sessions, activeSessionId]);

  // Load messages when the active session changes.
  useEffect(() => {
    let cancelled = false;
    if (!activeSessionId) {
      setLiveMessages([]);
      return;
    }
    setLiveMessages([]);
    setStreamError(null);
    sessionApi
      .getDetail(activeSessionId)
      .then((detail) => {
        if (cancelled) return;
        const mapped: Message[] = [];
        for (const rec of detail.messages) {
          if (rec.role === 'user') mapped.push(userMessageToDisplay(rec));
          else {
            const agent = agents.find((a) => a.id === detail.session.agent_id);
            mapped.push(
              agentMessageToDisplay(rec, agent?.name ?? 'Agent', agent?.avatar ?? '🤖'),
            );
          }
        }
        setLiveMessages(mapped);
        // 检测未答的 interrupt（ask_clarification/confirm_workflow）：页面跳转后
        // SSE 中断导致 pendingInterruptRef 丢失，从历史恢复，使下次发送走 /resume。
        const pending = [...mapped].reverse().find((m) => m.isInterrupted);
        if (pending) pendingInterruptRef.current = { toolMsgId: pending.id };
      })
      .catch((e) => {
        if (!cancelled) setStreamError(`加载会话失败：${(e as Error).message}`);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, agents]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [liveMessages]);

  const refreshSessions = useCallback(() => {
    qc.invalidateQueries({ queryKey: sessionKeys.lists() });
  }, [qc]);

  const handleSelectSession = (id: string) => setActiveSessionId(id);

  const handleDeleteSession = async (e: MouseEvent, id: string) => {
    e.stopPropagation();
    try {
      await sessionApi.remove(id);
      if (activeSessionId === id) setActiveSessionId(null);
      refreshSessions();
    } catch (err) {
      setStreamError(`删除会话失败：${(err as Error).message}`);
    }
  };

  const handleStartNewWithAgent = async (agent: Agent) => {
    try {
      const sess = await sessionApi.create(agent.id, `${agent.name} 空间`);
      setShowAgentSelectModal(false);
      setShowDropdown(false);
      refreshSessions();
      setActiveSessionId(sess._id);
    } catch (err) {
      setStreamError(`创建会话失败：${(err as Error).message}`);
    }
  };

  /** Pick a file into the local queue (no network upload yet — that happens
   *  at send time in handleSendMessage, so text + files dispatch together). */
  const handleUploadFile = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = ''; // reset so the same file can be re-picked
    if (files.length) {
      setPendingFiles((prev) => [...prev, ...files]);
    }
  };

  /** Send a message and consume the SSE stream into liveMessages. */
  const handleSendMessage = async (e?: FormEvent, overrideText?: string) => {
    e?.preventDefault();
    const sessionId = activeSessionId;
    const agent = activeAgent;
    // Allow send with text only OR files only (matches legacy chat-panel.tsx).
    // overrideText 来自 ask_clarification 卡片的选项/内联回答（走 /resume）。
    const prompt = (overrideText ?? inputText).trim();
    if ((!prompt && pendingFiles.length === 0) || !sessionId || !agent || isStreaming) return;

    setInputText('');
    setStreamError(null);

    // ── Upload queued files BEFORE dispatching the text prompt, so text + files
    // travel together and the agent sees the file content in the same turn.
    // (frontend-studio previously uploaded at pick-time, which raced the text
    // send and left the file unattached.) Mirrors chat-panel.tsx:443-489. ──
    const uploadedFileIds: string[] = [];
    const uploadedPaths: string[] = [];
    // 捕获完整 FileRef（含 mime_type/size/name），用于立即在用户气泡内联渲染附件，
    // 不必等历史重载。source='upload' → ChatAttachmentCard 走 getFileBlob(id) 取数。
    const uploadedAttachments: ChatAttachment[] = [];
    if (pendingFiles.length > 0) {
      setUploading(true);
      try {
        for (const f of pendingFiles) {
          const res = await sessionApi.uploadFile(sessionId, f);
          if (res.workspace_path) uploadedPaths.push(res.workspace_path);
          const id = getFileId(res.file);
          if (id) {
            uploadedFileIds.push(id);
            uploadedAttachments.push({
              source: 'upload',
              ref: id,
              name: res.file.name || f.name,
              mime_type: res.file.mime_type || f.type || undefined,
              size: res.file.size || f.size || undefined,
            });
          }
        }
      } catch (err) {
        setStreamError(`文件上传失败：${(err as Error).message}`);
        setUploading(false);
        return;
      } finally {
        setUploading(false);
      }
      setPendingFiles([]);
    }

    // If only files were attached with no text, the upload already persisted a
    // FileRef; refresh the session/files panels and stop here.
    if (!prompt) {
      refreshSessions();
      filesPanelRef.current?.refresh();
      return;
    }

    const userMsg: Message = {
      id: 'msg_user_' + Date.now(),
      senderName: '我',
      avatar: '👩‍💼',
      role: 'user',
      content: prompt,
      timestamp: '刚刚',
      // 立即内联渲染上传附件（图片直显 + 点击弹窗），不等历史重载
      attachments: uploadedAttachments.length > 0 ? uploadedAttachments : undefined,
    };

    // Placeholder agent bubble updated incrementally as deltas arrive.
    const agentMsgId = 'msg_stream_' + Date.now();
    const agentMsg: Message = {
      id: agentMsgId,
      senderName: agent.name,
      avatar: agent.avatar,
      role: 'agent',
      agentId: agent.id,
      content: '',
      timestamp: '刚刚',
      status: 'thinking',
      timeline: [],
    };
    // 重置 timeline 流式追踪 refs（让首轮 text 开新 entry）。
    textEntryIdRef.current = null;
    textStartedRef.current = false;
    deltaBufferRef.current = null;
    if (rafIdRef.current) {
      cancelAnimationFrame(rafIdRef.current);
      rafIdRef.current = null;
    }

    // ── 是否走 /resume：pendingInterruptRef 已设置（SSE interrupt）或存在等待中的
    // ask_clarification/confirm_workflow（页面跳转后从历史恢复）。回答注入对应卡片
    // 作为结果，不再单独渲染用户气泡；下次走 resume 让 agent 继续而非重复提问。
    const resumeMsgId =
      pendingInterruptRef.current?.toolMsgId ??
      [...liveMessages].reverse().find((m) => m.isInterrupted)?.id;
    const isResume = !!resumeMsgId;

    setLiveMessages((prev) => {
      if (isResume && resumeMsgId) {
        // 回答作为旧中断卡片的 timeline tool entry 的 result，关闭中断态；
        // 不渲染独立用户气泡。
        return prev
          .map((m) => {
            if (m.id !== resumeMsgId) return m;
            // 把 timeline 里第一个未答的 tool entry 填上用户回答。
            let answered = false;
            const tl = (m.timeline ?? []).map((e) => {
              if (
                !answered &&
                e.type === 'tool' &&
                (e.toolName === 'ask_clarification' || e.toolName === 'confirm_workflow') &&
                !e.result
              ) {
                answered = true;
                return { ...e, result: prompt, toolStatus: 'success' as const };
              }
              return e;
            });
            return { ...m, isInterrupted: false, timeline: tl };
          })
          .concat(agentMsg);
      }
      return [...prev, userMsg, agentMsg];
    });
    pendingInterruptRef.current = null;
    setIsStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = isResume
        ? await agentApi.resume(agent.id, {
            session_id: sessionId,
            answer: prompt,
            enable_thinking: enableThinking,
          })
        : await agentApi.stream(agent.id, {
            input: prompt,
            session_id: sessionId,
            enable_thinking: enableThinking,
            // file_ids: persists the reference on the user message (history).
            // file_paths: backend embeds file contents into the LLM user message
            // (agents.py:589-610) — without this the agent never sees the content.
            file_ids: uploadedFileIds.length > 0 ? uploadedFileIds : undefined,
            file_paths: uploadedPaths.length > 0 ? uploadedPaths : undefined,
          });
      // Attachments already handed off to the execution request above
      // (pendingFiles cleared earlier in this function).
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          detail = body?.error?.message ?? body?.message ?? detail;
        } catch {
          /* ignore */
        }
        throw new Error(detail);
      }

      // 累积流式过程中 agent 产出的 output 文件路径，挂到 agent 消息上内联预览。
      const streamedAttachments: ChatAttachment[] = [];
      // 累积 thinking 文本（thinking_delta 逐步到达），写进 timeline thinking entry。
      let thinkingText = '';

      for await (const evt of parseSSEStream(res)) {
        if (controller.signal.aborted) break;
        // StreamDoneEvent has no `type` field (it carries `done: true`); every
        // other event has one. Handle the done-event first so the switch below
        // narrows the discriminated union cleanly on `evt.type`.
        if (!('type' in evt)) {
          // Stream finished — refresh sessions so persisted messages/timeline load,
          // and reload generated files so any new outputs appear immediately.
          refreshSessions();
          filesPanelRef.current?.refresh();
          // 收敛残留 pending/running 的 tool entry 为 success（跳过 ask_clarification/
          // confirm_workflow —— 它们 interrupt 等待用户），并挂 usage。
          setLiveMessages((prev) =>
            prev.map((m) => {
              if (m.id !== agentMsgId) return m;
              const tl = (m.timeline ?? []).map((e) =>
                e.type === 'tool' &&
                (e.toolStatus === 'pending' || e.toolStatus === 'running') &&
                e.toolName !== 'ask_clarification' &&
                e.toolName !== 'confirm_workflow' &&
                e.toolName !== ''
                  ? { ...e, toolStatus: 'success' as const }
                  : e,
              );
              return { ...m, status: undefined, timeline: tl, usage: evt.usage ?? m.usage };
            }),
          );
          continue;
        }
        switch (evt.type) {
          case 'thinking':
          case 'thinking_delta': {
            thinkingText += evt.content;
            const text = thinkingText;
            setLiveMessages((prev) =>
              prev.map((m) => {
                if (m.id !== agentMsgId) return m;
                const tl = [...(m.timeline ?? [])];
                // 更新最后一个 thinking entry，没有就 push。
                const lastIdx = tl.length - 1;
                if (lastIdx >= 0 && tl[lastIdx].type === 'thinking') {
                  tl[lastIdx] = { ...tl[lastIdx], content: text };
                } else {
                  tl.push({ id: `${agentMsgId}-think`, type: 'thinking', content: text });
                }
                return { ...m, status: 'thinking', timeline: tl };
              }),
            );
            break;
          }
          case 'tool_call_start': {
            // 先同步 flush 文本 buffer（让 text 出现在 tool 之前），再 push 一个
            // pending tool entry，并重置 text refs（让 tool 之后的新 text 开新 entry）。
            if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
            const buf = deltaBufferRef.current;
            deltaBufferRef.current = null;
            rafIdRef.current = null;
            textEntryIdRef.current = null;
            textStartedRef.current = false;
            // tool_call_start 不带 tool_name（名字在后续 tool_call 事件里），
            // 先 push 一个 pending entry（toolName 空），tool_call 到达时补全。
            const toolName = '';
            const toolEntryId = `${agentMsgId}-tool-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
            setLiveMessages((prev) =>
              prev.map((m) => {
                if (m.id !== agentMsgId) return m;
                const tl = [...(m.timeline ?? [])];
                if (buf) tl.push({ id: `${agentMsgId}-text-${Date.now()}`, type: 'text', content: buf.delta });
                tl.push({ id: toolEntryId, type: 'tool', content: '', toolName, toolStatus: 'pending' });
                return { ...m, status: undefined, timeline: tl };
              }),
            );
            break;
          }
          case 'tool_call': {
            // 升级 pending tool entry 为 running（填入完整 args），没有就新建。
            if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
            deltaBufferRef.current = null;
            rafIdRef.current = null;
            textEntryIdRef.current = null;
            textStartedRef.current = false;
            setLiveMessages((prev) =>
              prev.map((m) => {
                if (m.id !== agentMsgId) return m;
                const tl = [...(m.timeline ?? [])];
                const targetIdx = tl.findIndex(
                  (e) => e.type === 'tool' && e.toolStatus === 'pending',
                );
                if (targetIdx >= 0) {
                  tl[targetIdx] = {
                    ...tl[targetIdx],
                    toolName: evt.tool_name,
                    args: evt.args,
                    toolStatus: 'running',
                  };
                } else {
                  tl.push({
                    id: `${agentMsgId}-tool-${Date.now()}`,
                    type: 'tool',
                    content: '',
                    toolName: evt.tool_name,
                    args: evt.args,
                    toolStatus: 'running',
                  });
                }
                return { ...m, timeline: tl };
              }),
            );
            break;
          }
          case 'tool_result': {
            const resultContent = evt.content;
            const isError = evt.status === 'error';
            // 解析工具结果里产出的 output 文件，累积到 agent 消息（去重）。
            const newAtts = parseOutputAttachments(resultContent);
            if (newAtts.length > 0) {
              for (const a of newAtts) {
                if (!streamedAttachments.some((b) => b.ref === a.ref)) {
                  streamedAttachments.push(a);
                }
              }
            }
            setLiveMessages((prev) =>
              prev.map((m) => {
                if (m.id !== agentMsgId) return m;
                const tl = [...(m.timeline ?? [])];
                // 从后往前找匹配的 running tool entry，填入 result + 状态。
                for (let i = tl.length - 1; i >= 0; i--) {
                  if (
                    tl[i].type === 'tool' &&
                    tl[i].toolName === evt.tool_name &&
                    tl[i].toolStatus === 'running'
                  ) {
                    tl[i] = {
                      ...tl[i],
                      result: resultContent,
                      toolStatus: isError ? 'error' : 'success',
                    };
                    break;
                  }
                }
                return {
                  ...m,
                  timeline: tl,
                  attachments: streamedAttachments.length > 0 ? [...streamedAttachments] : m.attachments,
                };
              }),
            );
            break;
          }
          case 'text_delta':
            // 走 RAF 批处理（appendDelta/flushDelta），平滑且多轮顺序正确。
            appendDelta(agentMsgId, evt.content);
            setLiveMessages((prev) =>
              prev.map((m) => (m.id === agentMsgId ? { ...m, status: undefined } : m)),
            );
            break;
          case 'text': {
            // 权威全文：覆盖最后一个 text entry，没有就 push。
            const content = evt.content;
            if (content) {
              if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current);
              deltaBufferRef.current = null;
              rafIdRef.current = null;
              setLiveMessages((prev) =>
                prev.map((m) => {
                  if (m.id !== agentMsgId) return m;
                  const tl = [...(m.timeline ?? [])];
                  let found = false;
                  for (let i = tl.length - 1; i >= 0; i--) {
                    if (tl[i].type === 'text') {
                      tl[i] = { ...tl[i], content };
                      found = true;
                      break;
                    }
                  }
                  if (!found) tl.push({ id: `${agentMsgId}-text-${Date.now()}`, type: 'text', content });
                  return { ...m, status: undefined, timeline: tl };
                }),
              );
            }
            break;
          }
          case 'error': {
            // 不 throw 中断流：把错误记录到 agent 消息 timeline 的 error entry。
            // 错误可能来自中途（如某个工具的加载失败），后续仍可能有事件。
            const errContent = evt.content || '执行出错';
            setStreamError(errContent);
            setLiveMessages((prev) =>
              prev.map((m) => {
                if (m.id !== agentMsgId) return m;
                const tl = [...(m.timeline ?? [])];
                tl.push({ id: `${agentMsgId}-err-${Date.now()}`, type: 'error', content: `❌ ${errContent}` });
                return { ...m, status: 'error', timeline: tl };
              }),
            );
            break;
          }
          case 'interrupt': {
            // Agent 经 ask_clarification/confirm_workflow 暂停等待用户回答：
            // 标记该消息为中断态（卡片可交互），记录消息 id 使下次发送走 /resume。
            setLiveMessages((prev) =>
              prev.map((m) => {
                if (m.id !== agentMsgId) return m;
                // 把最后一个未答的 ask_clarification/confirm_workflow tool entry
                // 标为 success（卡片就绪可交互）。
                const tl = [...(m.timeline ?? [])];
                for (let i = tl.length - 1; i >= 0; i--) {
                  if (
                    tl[i].type === 'tool' &&
                    (tl[i].toolName === 'ask_clarification' || tl[i].toolName === 'confirm_workflow') &&
                    !tl[i].result
                  ) {
                    tl[i] = { ...tl[i], toolStatus: 'success' };
                    break;
                  }
                }
                return { ...m, isInterrupted: true, timeline: tl };
              }),
            );
            pendingInterruptRef.current = { toolMsgId: agentMsgId };
            break;
          }
          default:
            break;
        }
      }
      // 兜底 flush 残留 delta。
      if (rafIdRef.current) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      if (deltaBufferRef.current) flushDelta();
    } catch (err) {
      const msg = (err as Error).message || '流式请求失败';
      setStreamError(msg);
      setLiveMessages((prev) =>
        prev.map((m) => {
          if (m.id !== agentMsgId) return m;
          const tl = [...(m.timeline ?? [])];
          tl.push({ id: `${agentMsgId}-err-${Date.now()}`, type: 'error', content: `❌ ${msg}` });
          return { ...m, status: 'error', timeline: tl };
        }),
      );
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
      refreshSessions();
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
    setIsStreaming(false);
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-0 bg-[#121214] border-0 overflow-hidden relative w-full h-full min-h-0 flex-1">
      {/* 1. SIDEBAR: Conversations List */}
      <div className="lg:col-span-3 min-h-0 border-r border-[#27272a] bg-[#18181b]/80 flex flex-col justify-between">
        <div className="flex flex-col h-full min-h-0">
          <div className="px-4 h-16 border-b border-[#27272a] flex items-center justify-between relative">
            <div className="flex items-center gap-1.5 cursor-pointer">
              <span className="text-sm font-bold text-white font-sans">对话</span>
              <ChevronDown className="w-3.5 h-3.5 text-[#a1a1aa]" />
            </div>
            <div className="relative">
              <button
                onClick={() => setShowDropdown(!showDropdown)}
                className="w-9 h-9 rounded-full bg-[#27272a]/50 border border-[#27272a] flex items-center justify-center text-white hover:bg-[#1f1f23] transition-colors cursor-pointer"
              >
                <Plus className="w-4 h-4 text-emerald-400" />
              </button>
              {showDropdown && (
                <div className="absolute right-0 mt-2 w-56 bg-[#18181b] border border-[#27272a] rounded-xl shadow-2xl p-1 z-50 animate-fade-in text-xs font-sans">
                  <div className="px-3 py-2 text-[10px] text-[#71717a] border-b border-[#27272a] font-semibold uppercase tracking-wider">
                    新建工作专区
                  </div>
                  <button
                    onClick={() => {
                      setShowAgentSelectModal(true);
                      setShowDropdown(false);
                    }}
                    className="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#121214] text-[#fafafa] flex flex-col gap-0.5 transition cursor-pointer"
                  >
                    <span className="font-semibold flex items-center gap-1.5">
                      <Sparkles className="w-3.5 h-3.5 text-amber-400" />
                      新建项目
                    </span>
                    <span className="text-[10px] text-[#71717a]">选择协作 Agent 启动全新会话</span>
                  </button>
                  <button
                    onClick={() => {
                      setShowAgentSelectModal(true);
                      setShowDropdown(false);
                    }}
                    className="w-full text-left px-3 py-2.5 rounded-lg hover:bg-[#121214] text-[#fafafa] flex flex-col gap-0.5 transition border-t border-[#27272a] pt-2 cursor-pointer font-sans"
                  >
                    <span className="font-semibold text-emerald-400 flex items-center gap-1.5">
                      <Bot className="w-3.5 h-3.5 text-emerald-400 font-bold" />
                      选择 Agent 开始
                    </span>
                    <span className="text-[10px] text-[#71717a]">从真实后端创建会话</span>
                  </button>
                </div>
              )}
            </div>
          </div>

          <div className="flex-1 min-h-0 overflow-y-auto scrollbar-custom p-2 space-y-1">
            {sessionsLoading && (
              <div className="p-3 text-[10px] text-[#71717a] font-mono">加载会话中…</div>
            )}
            {!sessionsLoading && sessions.length === 0 && (
              <div className="p-3 text-[10px] text-[#71717a] font-sans">
                暂无会话。点击右上 + 选择 Agent 新建。
              </div>
            )}
            {sessions.map((sess) => {
              const infoAgent = agents.find((a) => a.id === sess.agent_id);
              const isActive = activeSessionId === sess._id;
              return (
                <div
                  key={sess._id}
                  onClick={() => handleSelectSession(sess._id)}
                  className={`p-3 rounded-lg flex items-start gap-3 transition-all relative group cursor-pointer select-none ${
                    isActive
                      ? theme === 'light'
                        ? 'bg-slate-200'
                        : 'bg-[#121214] border border-[#27272a]/70 shadow-lg'
                      : theme === 'light'
                        ? 'hover:bg-slate-100'
                        : 'hover:bg-[#121214]/50 border border-transparent'
                  }`}
                >
                  <div className="relative">
                    <div className="w-10 h-10 rounded-xl bg-[#121214] border border-[#27272a] flex items-center justify-center text-xl shadow-inner select-none overflow-hidden">
                      <BotAvatar avatar={infoAgent?.avatar} className="w-full h-full p-1 text-xl" />
                    </div>
                    <span className="absolute bottom-0 right-0 w-2.5 h-2.5 rounded-full bg-emerald-500 border-2 border-[#18181b]" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex justify-between items-center mb-0.5">
                      <span className="text-xs font-bold text-white truncate font-sans">
                        {sess.title || infoAgent?.name || '未命名会话'}
                      </span>
                      <span className="text-[10px] text-[#71717a] font-mono leading-none shrink-0">
                        {formatSessionTime(sess.updated_at)}
                      </span>
                    </div>
                    <p className="text-[11px] text-[#a1a1aa] truncate font-sans leading-relaxed">
                      {sess.message_count ?? 0} 条消息 · {infoAgent?.name ?? 'Agent'}
                    </p>
                  </div>
                  <button
                    onClick={(e) => handleDeleteSession(e, sess._id)}
                    className="absolute right-2 bottom-3 opacity-0 group-hover:opacity-100 p-1 text-slate-500 hover:text-rose-400 hover:bg-[#18181b] rounded-md transition"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              );
            })}
          </div>
        </div>

        <div className="p-3.5 border-t border-[#27272a] bg-[#121214] rounded-none">
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse" />
            <span className="text-[10px] text-[#71717a] font-mono">Agent Flow SSE: Connected</span>
          </div>
        </div>
      </div>

      {/* 2. CHAT STREAM PANEL */}
      <div className="lg:col-span-9 min-h-0 flex flex-col h-full bg-[#121214]">
        <div className="px-6 h-16 border-b border-[#27272a] bg-[#18181b]/50 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-[#121214] border border-[#27272a] text-xl flex items-center justify-center overflow-hidden">
              <BotAvatar avatar={activeAgent?.avatar} className="w-full h-full p-1 text-xl" />
            </div>
            <div>
              <h3 className="text-xs font-bold text-white flex items-center gap-1.5 font-sans">
                {activeSession?.title || activeAgent?.name || '选择一个会话'}
                <span className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 text-[10px] font-mono">
                  SSE 流式
                </span>
              </h3>
              <p className="text-[11px] text-[#71717a] font-sans">
                {modelLabel(activeAgent?.model)} • {activeAgent?.name ?? '—'}
              </p>
            </div>
          </div>
          {/* Chat / Files tab switcher */}
          <div className="flex items-center gap-1 p-0.5 rounded-lg bg-[#121214] border border-[#27272a]">
            {(['chat', 'files'] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setRightTab(tab)}
                className={`px-2.5 py-1 rounded-md text-[11px] font-semibold transition cursor-pointer ${
                  rightTab === tab ? 'bg-[#1E5EFF] text-white' : 'text-[#71717a] hover:text-white'
                }`}
              >
                {tab === 'chat' ? '对话' : '文件'}
              </button>
            ))}
          </div>
        </div>

        {streamError && (
          <div className="px-6 py-2 bg-rose-950/30 border-b border-rose-900/40 text-rose-400 text-[11px] font-sans">
            ⚠ {streamError}
          </div>
        )}

        {rightTab === 'files' ? (
          <SessionFilesPanel ref={filesPanelRef} sessionId={activeSessionId} />
        ) : (
        <div className="flex-1 min-h-0 overflow-y-auto scrollbar-custom p-6 space-y-5">
          <div className="flex items-center justify-center">
            <span className="px-3 py-1 rounded bg-[#18181b] border border-[#27272a]/60 text-[#71717a] text-[10px] font-mono">
              对话由 Agent Flow 引擎实时流式生成
            </span>
          </div>

          {!activeSession && !sessionsLoading && (
            <div className="flex flex-col items-center justify-center py-20 text-center">
              <Bot className="w-10 h-10 text-[#71717a] mb-3" />
              <p className="text-sm text-[#a1a1aa] font-sans">还没有会话</p>
              <p className="text-[11px] text-[#71717a] mt-1">
                点击左上 + 选择一个 Agent 开始对话
              </p>
            </div>
          )}

          {liveMessages.map((msg) => {
            const isUser = msg.role === 'user';
            const isAgent = msg.role === 'agent';
            // agent 消息靠 timeline 渲染（text/tool/thinking/error 按顺序）；
            // user 消息保持单个气泡。
            const hasTimeline = isAgent && !!msg.timeline && msg.timeline.length > 0;
            return (
              <div
                key={msg.id}
                className={`flex items-start gap-4 max-w-4xl animate-fade-in ${isUser ? 'ml-auto flex-row-reverse' : ''}`}
              >
                <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 select-none shadow ${
                  isUser
                    ? 'bg-[#4f46e5]/15 border border-[#4f46e5]/30 text-indigo-400'
                    : 'bg-[#18181b] border border-[#27272a] overflow-hidden'
                }`}>
                  {isUser ? (
                    <User className="w-4 h-4" />
                  ) : (
                    <BotAvatar avatar={msg.avatar} className="w-full h-full p-1.5 text-lg" />
                  )}
                </div>
                <div className="space-y-1 min-w-0 flex-1">
                  <div className={`flex items-center gap-2 text-[10px] ${isUser ? 'justify-end' : ''}`}>
                    {!isUser && (
                      <span className="font-bold text-[#a1a1aa] font-sans">
                        {msg.senderName}
                        {msg.status === 'thinking' && <span className="text-indigo-400 ml-1">· 思考中</span>}
                      </span>
                    )}
                    <span className="text-[#71717a] font-mono">{msg.timestamp}</span>
                    {!isUser && msg.usage?.total_tokens ? (
                      <span className="text-[#71717a] font-mono">
                        · {msg.usage.total_tokens.toLocaleString()} tokens
                        {msg.usage.llm_calls != null && msg.usage.llm_calls > 1
                          ? ` · ${msg.usage.llm_calls} 轮`
                          : ''}
                      </span>
                    ) : null}
                  </div>
                  {hasTimeline ? (
                    <div className="space-y-2 max-w-2xl">
                      {msg.timeline!.map((entry, idx) => {
                        const isLast = idx === msg.timeline!.length - 1;
                        if (entry.type === 'text') {
                          return (
                            <div
                              key={entry.id}
                              className="p-4 rounded-xl rounded-tl-none text-[12.5px] leading-relaxed border font-sans select-text shadow-sm bg-[#18181b] border-[#27272a] text-[#e4e4e7]"
                            >
                              <Markdown content={entry.content} />
                              {/* agent 消息的产出附件挂在最后一个 text entry 后面 */}
                              {isLast && msg.attachments && msg.attachments.length > 0 && (
                                <div className="flex flex-wrap gap-2 mt-3 pt-3 border-t border-current/10">
                                  {msg.attachments.map((att, i) => (
                                    <ChatAttachmentCard
                                      key={`${msg.id}-att-${i}-${att.ref}`}
                                      att={att}
                                      sessionId={activeSessionId}
                                    />
                                  ))}
                                </div>
                              )}
                            </div>
                          );
                        }
                        if (entry.type === 'thinking') {
                          return (
                            <ThinkingEntryCard
                              key={entry.id}
                              entry={entry}
                              onToggle={() =>
                                setLiveMessages((prev) =>
                                  prev.map((m) =>
                                    m.id === msg.id
                                      ? {
                                          ...m,
                                          timeline: (m.timeline ?? []).map((e) =>
                                            e.id === entry.id ? { ...e, expanded: !e.expanded } : e,
                                          ),
                                        }
                                      : m,
                                  ),
                                )
                              }
                            />
                          );
                        }
                        if (entry.type === 'error') {
                          return (
                            <div
                              key={entry.id}
                              className="p-4 rounded-xl rounded-tl-none text-[12.5px] leading-relaxed border font-sans shadow-sm bg-rose-950/20 border-rose-900/40 rounded-tl-none text-rose-300"
                            >
                              {entry.content}
                            </div>
                          );
                        }
                        // tool entry
                        return (
                          <ToolEntryCard
                            key={entry.id}
                            entry={entry}
                            msgId={msg.id}
                            interrupted={!!msg.isInterrupted}
                            onAnswer={(a) => handleSendMessage(undefined, a)}
                          />
                        );
                      })}
                    </div>
                  ) : (
                  <div
                    className={`p-4 rounded-xl text-[12.5px] leading-relaxed border font-sans select-text shadow-sm ${
                      msg.status === 'thinking'
                        ? 'bg-indigo-950/20 border-indigo-900/40 rounded-tl-none text-indigo-300 italic'
                        : isUser
                          ? 'bg-[#4f46e5]/10 border-[#4f46e5]/30 rounded-tr-none text-[#fafafa]'
                          : 'bg-[#18181b] border-[#27272a] rounded-tl-none text-[#e4e4e7]'
                    }`}
                  >
                    {msg.content
                      ? <Markdown content={msg.content} />
                      : (msg.status === 'thinking' ? '…' : '')}
                    {/* 可预览附件挂在气泡内部（紧随正文，非独立一行）。 */}
                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className={`flex flex-wrap gap-2 mt-3 pt-3 border-t border-current/10 ${isUser ? 'justify-end' : ''}`}>
                        {msg.attachments.map((att, i) => (
                          <ChatAttachmentCard
                            key={`${msg.id}-att-${i}-${att.ref}`}
                            att={att}
                            sessionId={activeSessionId}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                  )}
                </div>
              </div>
            );
          })}
          <div ref={messageEndRef} />
        </div>
        )}

        {/* Input box (chat tab only) */}
        {rightTab === 'chat' && (
        <div className="p-4 border-t border-[#27272a]">
          <form onSubmit={handleSendMessage} className="relative bg-[#18181b] border border-[#27272a] rounded-xl p-3 flex flex-col justify-between">
            {pendingFiles.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {pendingFiles.map((f, i) => (
                  <span key={`${f.name}-${i}`} className="flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/30 text-[10px] text-emerald-300 font-sans">
                    <FileCode className="w-3 h-3" />
                    <span className="max-w-[140px] truncate">{f.name}</span>
                    <button
                      type="button"
                      onClick={() => setPendingFiles((prev) => prev.filter((_, j) => j !== i))}
                      className="hover:text-white cursor-pointer"
                    >
                      <X className="w-2.5 h-2.5" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <textarea
              rows={2}
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              placeholder={
                activeSession
                  ? '跟我说说你的偏好和要求，我会更懂你（Enter 发送，Shift+Enter 换行）'
                  : '请先在左侧选择或新建一个会话'
              }
              disabled={!activeSession || isStreaming}
              className="w-full bg-transparent text-xs text-white focus:outline-none resize-none font-sans placeholder-[#71717a] leading-relaxed disabled:opacity-50"
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSendMessage();
                }
              }}
            />
            <div className="flex items-center justify-between border-t border-[#27272a]/50 pt-2.5 mt-2">
              <div className="flex items-center gap-2">
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  onChange={handleUploadFile}
                  disabled={!activeSessionId || uploading || isStreaming}
                />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={!activeSessionId || uploading || isStreaming}
                  className="w-6 h-6 rounded-full bg-[#121214] border border-[#27272a] flex items-center justify-center text-white hover:bg-[#27272a] hover:text-emerald-400 transition disabled:opacity-40"
                  title="上传文件附件"
                >
                  {uploading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Paperclip className="w-3.5 h-3.5" />}
                </button>
                {/* Thinking-mode toggle */}
                <button
                  type="button"
                  onClick={() => setEnableThinking((v) => !v)}
                  className={`flex items-center gap-1 px-2 py-0.5 rounded-full border text-[10px] font-semibold transition cursor-pointer select-none ${
                    enableThinking
                      ? 'bg-violet-500/15 border-violet-500/40 text-violet-300'
                      : 'bg-[#121214] border-[#27272a] text-[#71717a] hover:text-white'
                  }`}
                  title="思考模式（enable_thinking）— 流式返回思考过程"
                >
                  <Brain className="w-3 h-3" />
                  {enableThinking ? '思考' : '直答'}
                </button>
                <span className="text-[10px] text-[#71717a] font-sans flex items-center gap-1 select-none">
                  <CheckCircle className="w-3 h-3 text-indigo-400" />
                  {isStreaming ? '正在接收流…' : uploading ? '上传中…' : '就绪'}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1 bg-[#121214] border border-[#27272a] rounded px-2 py-0.5 text-[10px] font-mono text-[#a1a1aa] select-none">
                  <span>{modelLabel(activeAgent?.model)}</span>
                </div>
                {isStreaming ? (
                  <button
                    type="button"
                    onClick={handleStop}
                    className="w-7 h-7 rounded-full flex items-center justify-center cursor-pointer bg-rose-600 hover:bg-rose-500 text-white"
                    title="中断"
                  >
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  </button>
                ) : (
                  <button
                    type="submit"
                    disabled={(!inputText.trim() && pendingFiles.length === 0) || !activeSession}
                    className={`w-7 h-7 rounded-full flex items-center justify-center cursor-pointer transition ${
                      (inputText.trim() || pendingFiles.length > 0) && activeSession
                        ? 'bg-indigo-600 hover:bg-indigo-500 text-white shadow-md'
                        : 'bg-[#27272a] text-[#71717a]'
                    }`}
                  >
                    <Send className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            </div>
          </form>
        </div>
        )}
      </div>

      {/* 3. SELECT AGENT FOR NEW SESSION MODAL */}
      {showAgentSelectModal && (
        <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4 z-50 animate-fade-in text-xs font-sans">
          <div className="w-full max-w-lg bg-[#18181b] border border-[#27272a] rounded-xl shadow-2xl relative overflow-hidden">
            <div className="p-4 border-b border-[#27272a] flex justify-between items-center bg-[#121214]/60">
              <h3 className="text-sm font-bold text-white flex items-center gap-1.5 font-sans">
                <Sparkles className="w-4 h-4 text-emerald-400" />
                选择协作 Agent 开始会话
              </h3>
              <button
                onClick={() => setShowAgentSelectModal(false)}
                className="text-[#71717a] hover:text-white font-bold cursor-pointer"
              >
                ✕
              </button>
            </div>
            <div className="p-5 max-h-[400px] overflow-y-auto grid grid-cols-1 md:grid-cols-2 gap-3">
              {agents.length === 0 && (
                <div className="col-span-full text-center text-[#71717a] py-8">
                  暂无可用 Agent，请先在 Agent 空间创建。
                </div>
              )}
              {agents.map((agent) => (
                <div
                  key={agent.id}
                  onClick={() => handleStartNewWithAgent(agent)}
                  className="p-3 border border-[#27272a] bg-[#121214]/50 rounded-xl hover:border-emerald-500/50 hover:bg-[#121214] cursor-pointer transition flex gap-3"
                >
                  <div className="w-10 h-10 rounded-xl bg-[#18181b] border border-[#27272a] text-xl flex items-center justify-center shrink-0 shadow-inner select-none overflow-hidden">
                    <BotAvatar avatar={agent.avatar} className="w-full h-full p-1 text-xl" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <h4 className="font-bold text-white truncate">{agent.name}</h4>
                    <p className="text-[10px] text-[#71717a] line-clamp-2 mt-0.5 leading-normal">
                      {agent.description}
                    </p>
                  </div>
                </div>
              ))}
            </div>
            <div className="p-4 border-t border-[#27272a] bg-[#121214] flex justify-end">
              <button
                onClick={() => setShowAgentSelectModal(false)}
                className="px-4 py-2 border border-[#27272a] hover:bg-[#18181b] text-slate-400 hover:text-white rounded-lg cursor-pointer font-semibold"
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function updateMsg(
  prev: Message[],
  id: string,
  patch: Partial<Message>,
): Message[] {
  return prev.map((m) => (m.id === id ? { ...m, ...patch } : m));
}

function formatSessionTime(iso: string): string {
  try {
    const d = new Date(iso);
    const now = Date.now();
    const diff = now - d.getTime();
    if (diff < 60_000) return '刚刚';
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}分钟前`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}小时前`;
    return d.toLocaleDateString();
  } catch {
    return '';
  }
}

/* ────────────────────────────────────────────────────────────
   Tool-call rendering helpers + ToolCallCard
   Mirrors the structured tool card from frontend/src/components/chat-panel.tsx:
   a status-colored header (running/success/error) with a collapsible detail
   pane showing formatted request args + return result.
   ──────────────────────────────────────────────────────────── */

/** Per-status visual config. Uses Tailwind opacity color tokens so it renders
 *  correctly in both dark and light themes (no raw hex). `hover` deepens the
 *  same status color slightly (→ /15) for a subtle, theme-safe lift instead of
 *  overlaying an unrelated neutral that looks jarring on a colored card. */
const TOOL_STATUS_CFG: Record<
  NonNullable<Message['toolStatus']>,
  { wrap: string; hover: string; icon: string; label: string }
> = {
  pending: {
    wrap: 'bg-amber-500/10 border-amber-500/30',
    hover: 'hover:bg-amber-500/15',
    icon: 'text-amber-400',
    label: '正在生成参数',
  },
  running: {
    wrap: 'bg-amber-500/10 border-amber-500/30',
    hover: 'hover:bg-amber-500/15',
    icon: 'text-amber-400',
    label: '执行中…',
  },
  success: {
    wrap: 'bg-emerald-500/10 border-emerald-500/30',
    hover: 'hover:bg-emerald-500/15',
    icon: 'text-emerald-400',
    label: '已完成',
  },
  error: {
    wrap: 'bg-rose-500/10 border-rose-500/30',
    hover: 'hover:bg-rose-500/15',
    icon: 'text-rose-400',
    label: '失败',
  },
};

/** Pretty-print a tool's request args. Falls back to String() on failure. */
function formatToolArgs(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return '';
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

/** Try to pretty-print a tool result. If it parses as JSON, re-serialize it
 *  indented; otherwise return the raw string. */
function formatToolResult(raw?: string): { text: string; isJson: boolean } {
  if (!raw) return { text: '', isJson: false };
  const trimmed = raw.trim();
  if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) {
    return { text: raw, isJson: false };
  }
  try {
    return { text: JSON.stringify(JSON.parse(trimmed), null, 2), isJson: true };
  } catch {
    return { text: raw, isJson: false };
  }
}

const RESULT_COLLAPSE_THRESHOLD = 800;

/** Thinking entry — collapsible reasoning card. */
function ThinkingEntryCard({
  entry,
  onToggle,
}: {
  entry: TimelineEntry;
  onToggle: () => void;
}) {
  const expanded = !!entry.expanded;
  return (
    <div className="rounded-lg border border-blue-100 bg-blue-50/50 overflow-hidden font-sans dark:border-indigo-900/40 dark:bg-indigo-950/20">
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2 border-0 bg-transparent cursor-pointer text-left hover:bg-blue-50 dark:hover:bg-indigo-950/40 transition-colors"
      >
        <Brain className="text-indigo-400" size={13} />
        <span className="text-xs font-medium text-indigo-300">思考过程</span>
        <span className="text-[10px] text-indigo-400/60 ml-auto">{expanded ? '收起' : '展开'}</span>
        <ChevronRight className={`w-3 h-3 text-indigo-400/60 transition-transform ${expanded ? 'rotate-90' : ''}`} />
      </button>
      {expanded && (
        <div className="px-3 pb-2 pt-2 text-xs text-indigo-300/80 whitespace-pre-wrap leading-relaxed border-t border-indigo-900/40 italic">
          {entry.content}
        </div>
      )}
    </div>
  );
}

/** Tool entry card — renders a timeline tool entry. Dispatches to
 *  ClarificationCard / WorkflowProposalCard / WorkflowTaskCard for known
 *  tool types, else a generic collapsible tool card. Reads entry fields
 *  (toolName/args/result/toolStatus) instead of flat message fields. */
function ToolEntryCard({
  entry,
  msgId,
  interrupted,
  onAnswer,
}: {
  entry: TimelineEntry;
  msgId: string;
  interrupted: boolean;
  onAnswer?: (answer: string) => void;
}) {
  // ask_clarification 走专门的交互卡片（按 clarification_type 分样式）。
  if (entry.toolName === 'ask_clarification') {
    return <ClarificationCard entry={entry} interrupted={interrupted} onAnswer={onAnswer} />;
  }
  // confirm_workflow：工作流确认卡片（从 tool_call args 渲染，tool_result 决定终态）。
  if (entry.toolName === 'confirm_workflow') {
    const args = entry.args ?? {};
    const resultText = entry.result ?? '';
    const isRejection = /取消|拒绝|cancel/i.test(resultText);
    const forceAction = entry.result ? (isRejection ? 'rejected' : 'confirmed') : undefined;
    return (
      <WorkflowProposalCard
        proposal={{
          type: 'workflow_proposal',
          workflow_name: String(args.workflow_name ?? ''),
          workflow_description: String(args.description ?? ''),
          input_preview: (args.params ?? {}) as Record<string, unknown>,
        }}
        forceAction={forceAction}
        onConfirm={(wfName) => {
          onAnswer?.(`确认执行 ${wfName}`);
          return true;
        }}
      />
    );
  }
  // dispatch_workflow：解析 task_created → 内嵌可展开 task 卡片。
  if (entry.toolName === 'dispatch_workflow') {
    const created = parseTaskCreated(entry.result);
    if (created) return <WorkflowTaskCard created={created} />;
  }
  const status = entry.toolStatus ?? 'running';
  const cfg = TOOL_STATUS_CFG[status];
  const [expanded, setExpanded] = useState(false);
  const [resultExpanded, setResultExpanded] = useState(false);

  const argsText = formatToolArgs(entry.args);
  const result = formatToolResult(entry.result);
  const hasDetail = Boolean(argsText || result.text);
  const resultTooLong = result.text.length > RESULT_COLLAPSE_THRESHOLD;
  const shownResult =
    !resultExpanded && resultTooLong
      ? result.text.slice(0, RESULT_COLLAPSE_THRESHOLD) + '…'
      : result.text;

  const StatusIcon =
    status === 'running' || status === 'pending' ? Loader2 : status === 'success' ? CheckCircle : AlertTriangle;

  return (
    <div className={`rounded-xl rounded-tl-none border overflow-hidden font-sans ${cfg.wrap}`}>
      {/* Header — click to toggle detail */}
      <button
        type="button"
        onClick={() => hasDetail && setExpanded((v) => !v)}
        className={`w-full flex items-center gap-2 px-3.5 py-2.5 bg-transparent border-0 text-left transition-colors ${
          hasDetail ? `cursor-pointer ${cfg.hover}` : 'cursor-default'
        }`}
      >
        <StatusIcon
          className={`w-3.5 h-3.5 shrink-0 ${cfg.icon} ${status === 'running' || status === 'pending' ? 'animate-spin' : ''}`}
        />
        <Wrench className={`w-3.5 h-3.5 shrink-0 ${cfg.icon}`} />
        <span className={`text-xs font-semibold truncate ${cfg.icon}`}>
          {entry.toolName || 'unknown_tool'}
        </span>
        <span className={`text-[10px] ${cfg.icon} opacity-70`}>{cfg.label}</span>
        {hasDetail && (
          <span className={`ml-auto flex items-center gap-1 text-[10px] ${cfg.icon} opacity-70`}>
            {expanded ? '收起' : '详情'}
            <ChevronRight
              className={`w-3 h-3 transition-transform ${expanded ? 'rotate-90' : ''}`}
            />
          </span>
        )}
      </button>

      {/* Detail pane */}
      {expanded && hasDetail && (
        <div className="border-t border-[#27272a] px-3.5 pb-3 pt-2.5 space-y-2.5">
          {argsText && (
            <div>
              <div className={`text-[10px] font-semibold mb-1 opacity-60 ${cfg.icon}`}>
                请求参数
              </div>
              <pre className="text-[11px] leading-relaxed whitespace-pre-wrap break-all rounded-lg p-2 bg-[#121214] border border-[#27272a] text-[#a1a1aa] font-mono max-h-48 overflow-y-auto">
                {argsText}
              </pre>
            </div>
          )}
          {result.text && (
            <div>
              <div className={`text-[10px] font-semibold mb-1 opacity-60 ${cfg.icon} flex items-center gap-1.5`}>
                返回结果
                {result.isJson && (
                  <span className="px-1 py-0 rounded bg-[#27272a] text-[9px] opacity-80">JSON</span>
                )}
              </div>
              <pre className="text-[11px] leading-relaxed whitespace-pre-wrap break-all rounded-lg p-2 bg-[#121214] border border-[#27272a] text-[#a1a1aa] font-mono max-h-64 overflow-y-auto">
                {shownResult}
              </pre>
              {resultTooLong && (
                <button
                  type="button"
                  onClick={() => setResultExpanded((v) => !v)}
                  className={`mt-1 text-[10px] underline ${cfg.icon} opacity-80 hover:opacity-100 cursor-pointer`}
                >
                  {resultExpanded ? '收起结果' : `展开全部 (${result.text.length} 字符)`}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ────────────────────────────────────────────────────────────
   ClarificationCard — ask_clarification 交互卡片
   按 clarification_type 分样式渲染问题 + 选项/按钮 + 内联输入。
   交互态（!answered && clarificationActive）选项可点、内联可填；已回答后选项
   静态高亮、答案以 indigo 气泡附在卡片下方。暗色配色用 Tailwind opacity token，
   明暗主题通用。对照 frontend/src/components/chat-panel.tsx 的 ask_clarification 渲染。
   ──────────────────────────────────────────────────────────── */
function ClarificationCard({
  entry,
  interrupted,
  onAnswer,
}: {
  entry: TimelineEntry;
  interrupted: boolean;
  onAnswer?: (answer: string) => void;
}) {
  const args = entry.args ?? {};
  const question = args.question ? String(args.question) : '';
  // LLM 可能把 options 传成 JSON 字符串，做一次兼容解析。
  const rawOptions = args.options;
  let options: string[] = [];
  if (Array.isArray(rawOptions)) options = rawOptions as string[];
  else if (typeof rawOptions === 'string') {
    try {
      options = JSON.parse(rawOptions);
    } catch {
      /* ignore */
    }
  }
  const clarificationType = args.clarification_type
    ? String(args.clarification_type)
    : 'missing_info';
  const answered = !!entry.result;
  // 交互态：未回答 + 消息处于中断态（interrupt 挂起中，或历史回填恢复的中断）。
  const interactive = !answered && interrupted;

  // fields 向导模式：ask_clarification 带 fields 时渲染多步结构化表单，
  // 单次收集多个字段。fields 可能是数组或 JSON 字符串（LLM 容错）。
  const rawFields = args.fields;
  let formFields: ClarificationField[] = [];
  if (Array.isArray(rawFields)) formFields = rawFields as ClarificationField[];
  else if (typeof rawFields === 'string') {
    try {
      const parsed = JSON.parse(rawFields);
      if (Array.isArray(parsed)) formFields = parsed as ClarificationField[];
    } catch {
      /* ignore */
    }
  }
  if (formFields.length > 0) {
    return (
      <ClarificationFormCard
        question={question}
        context={args.context ? String(args.context) : undefined}
        fields={formFields}
        answered={answered}
        result={entry.result}
        onSubmit={(jsonStr) => onAnswer?.(jsonStr)}
      />
    );
  }

  const [inlineText, setInlineText] = useState('');
  const submitInline = () => {
    const t = inlineText.trim();
    if (!t) return;
    onAnswer?.(t);
    setInlineText('');
  };

  const isRisk = clarificationType === 'risk_confirmation';
  const isSuggestion = clarificationType === 'suggestion';
  const verticalOptions =
    clarificationType === 'approach_choice' || clarificationType === 'ambiguous_requirement';

  const palette = isRisk
    ? { wrap: 'bg-amber-500/10 border-amber-500/30', q: 'text-amber-200', icon: '⚠️' }
    : isSuggestion
      ? { wrap: 'bg-emerald-500/10 border-emerald-500/30', q: 'text-emerald-200', icon: '💡' }
      : { wrap: 'bg-indigo-500/10 border-indigo-500/30', q: 'text-indigo-200', icon: '❓' };

  return (
    <div className="flex flex-col gap-2">
      <div className={`rounded-xl rounded-tl-none border px-4 py-3 font-sans ${palette.wrap}`}>
        <div className="flex items-start gap-2.5">
          <span className="text-sm mt-0.5 select-none">{palette.icon}</span>
          <div className="flex-1 min-w-0">
            <div className={`text-[13px] whitespace-pre-wrap leading-relaxed ${palette.q}`}>
              {question}
            </div>

            {options.length > 0 && (
              <div className={`flex gap-2 mt-3 ${verticalOptions ? 'flex-col' : 'flex-wrap'}`}>
                {options.map((opt, i) =>
                  interactive ? (
                    <button
                      key={i}
                      type="button"
                      onClick={() => onAnswer?.(opt)}
                      className="text-left px-3 py-1.5 rounded-lg text-xs border bg-[#121214] border-[#27272a] text-slate-200 hover:border-indigo-500/50 hover:bg-indigo-500/10 transition cursor-pointer"
                    >
                      {opt}
                    </button>
                  ) : (
                    <div
                      key={i}
                      className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                        entry.result === opt
                          ? 'bg-indigo-500/15 border-indigo-500/50 text-indigo-200 font-medium'
                          : 'bg-[#121214]/60 border-[#27272a] text-[#71717a]'
                      }`}
                    >
                      {opt}
                      {entry.result === opt && <span className="ml-1.5 text-indigo-400">✓</span>}
                    </div>
                  ),
                )}
              </div>
            )}

            {/* risk_confirmation：确认执行 / 取消 */}
            {interactive && isRisk && (
              <div className="flex gap-2 mt-3">
                <button
                  type="button"
                  onClick={() => onAnswer?.('确认')}
                  className="px-3.5 py-1.5 rounded-lg text-xs bg-rose-600 text-white hover:bg-rose-500 transition cursor-pointer"
                >
                  确认执行
                </button>
                <button
                  type="button"
                  onClick={() => onAnswer?.('取消')}
                  className="px-3.5 py-1.5 rounded-lg text-xs bg-[#121214] border border-[#27272a] text-slate-300 hover:bg-[#27272a] transition cursor-pointer"
                >
                  取消
                </button>
              </div>
            )}

            {/* suggestion：接受按钮 */}
            {interactive && isSuggestion && (
              <div className="flex gap-2 mt-3 items-center">
                <button
                  type="button"
                  onClick={() => onAnswer?.('接受建议')}
                  className="px-3.5 py-1.5 rounded-lg text-xs bg-emerald-600 text-white hover:bg-emerald-500 transition cursor-pointer"
                >
                  接受
                </button>
                <span className="text-[11px] text-[#71717a]">或输入其他回答</span>
              </div>
            )}

            {/* 内联自由输入（交互态通用兜底） */}
            {interactive && (
              <div className="flex gap-1.5 mt-2.5">
                <input
                  type="text"
                  value={inlineText}
                  onChange={(e) => setInlineText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      submitInline();
                    }
                  }}
                  placeholder="输入你的回答…"
                  className="flex-1 min-w-0 px-2.5 py-1.5 rounded-lg text-xs border border-[#27272a] bg-[#121214] text-slate-200 placeholder:text-[#52525b] focus:outline-none focus:border-indigo-500/50"
                />
                <button
                  type="button"
                  onClick={submitInline}
                  disabled={!inlineText.trim()}
                  className="px-2.5 py-1.5 rounded-lg text-xs bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-40 transition cursor-pointer"
                >
                  发送
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 用户答案：indigo 右对齐气泡 */}
      {answered && (
        <div className="flex flex-row-reverse">
          <div className="max-w-[75%] rounded-xl rounded-tr-sm px-3 py-2 text-[13px] leading-relaxed bg-indigo-500/15 border border-indigo-500/30 text-indigo-200 whitespace-pre-wrap">
            {entry.result}
          </div>
        </div>
      )}
    </div>
  );
}

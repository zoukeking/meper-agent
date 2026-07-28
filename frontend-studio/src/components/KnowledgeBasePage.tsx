/**
 * KnowledgeBasePage — KB card list + create dialog.
 *
 * Cards show name / description / file_count / total_size. Click a card to
 * open KbDetailPage (file management). Modelled on AgentSpace.
 */
import { useState, type FormEvent } from 'react';
import { Plus, BookOpen, Trash2, Loader2, FileText } from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { knowledgeApi, knowledgeKeys } from '../services/knowledge-api';
import { confirmDialog } from './ui/confirm';
import { toast } from './ui/toast';

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function KnowledgeBasePage({
  onOpenKb,
}: {
  onOpenKb: (kb: { id: string; name: string }) => void;
}) {
  const queryClient = useQueryClient();
  const [isCreating, setIsCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: knowledgeKeys.list({}),
    queryFn: () => knowledgeApi.list({ page: 1, page_size: 50 }),
  });
  const kbs = data?.items ?? [];

  const createM = useMutation({
    mutationFn: (input: { name: string; description?: string }) => knowledgeApi.create(input),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      setError(null);
      setIsCreating(false);
      setNewName('');
      setNewDesc('');
      onOpenKb({ id: created.id, name: created.name });
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : '创建失败'),
  });

  const deleteM = useMutation({
    mutationFn: (id: string) => knowledgeApi.remove(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.all });
      toast.success('知识库已删除');
    },
    onError: (e: unknown) => toast.error(e instanceof Error ? e.message : '删除失败'),
  });

  const handleDelete = async (id: string, name: string) => {
    const ok = await confirmDialog({
      title: `删除知识库「${name}」？`,
      description: '知识库内所有文件将被清除。若被 Agent 引用将拒绝删除。',
      okText: '删除',
      danger: true,
    });
    if (ok) deleteM.mutate(id);
  };

  const handleCreateSave = (e: FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    createM.mutate({ name: newName.trim(), description: newDesc.trim() || undefined });
  };

  return (
    <div className="space-y-6">
      {/* header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between border-b border-[#27272a] pb-4 gap-4">
        <div className="space-y-0.5">
          <h2 className="text-sm font-bold text-[#fafafa] flex items-center gap-2">
            <BookOpen className="w-4 h-4 text-indigo-400" />
            知识库 ({kbs.length})
          </h2>
          <p className="text-xs text-[#71717a]">
            Markdown 文档库，绑定到 Agent 后可用 kb_glob / kb_grep / kb_read 实时探索。
          </p>
        </div>
        <button
          onClick={() => setIsCreating(true)}
          className="px-4 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg transition flex items-center gap-1.5 cursor-pointer"
        >
          <Plus className="w-4 h-4 text-emerald-400 font-bold" />
          创建知识库
        </button>
      </div>

      {error && (
        <div className="px-3 py-2 rounded-lg bg-rose-950/30 border border-rose-700/40 text-rose-300 text-xs">{error}</div>
      )}

      {isLoading ? (
        <p className="text-xs text-[#71717a]">加载中…</p>
      ) : kbs.length === 0 ? (
        <p className="text-xs text-[#71717a]">还没有知识库，点击右上角创建。</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          {kbs.map((kb) => (
            <div
              key={kb.id}
              onClick={() => onOpenKb({ id: kb.id, name: kb.name })}
              className="bg-[#18181b] border border-[#27272a] rounded-xl overflow-hidden relative group hover:border-[#3f3f46] transition cursor-pointer"
            >
              <div className="p-5 space-y-3">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3">
                    <div className="w-11 h-11 bg-[#121214] rounded-xl flex items-center justify-center border border-[#27272a]">
                      <BookOpen className="w-5 h-5 text-indigo-400" />
                    </div>
                    <div className="space-y-0.5">
                      <h4 className="text-sm font-bold text-white">{kb.name}</h4>
                      <span className="text-[10px] text-[#71717a] font-mono">{kb.id}</span>
                    </div>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDelete(kb.id, kb.name); }}
                    className="p-1 px-1.5 bg-[#121214] border border-[#27272a] rounded-lg text-rose-500 hover:text-rose-400 hover:bg-rose-950/20 opacity-0 group-hover:opacity-100 transition cursor-pointer"
                    title="删除"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
                <p className="text-xs text-[#a1a1aa] leading-relaxed min-h-[32px] line-clamp-2">{kb.description || '（无描述）'}</p>
                <div className="flex items-center gap-3 text-[10px] text-[#71717a] font-mono">
                  <span className="flex items-center gap-1"><FileText className="w-3 h-3" />{kb.file_count} 文件</span>
                  <span>{formatSize(kb.total_size)}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* create modal */}
      {isCreating && (
        <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4 z-50 text-xs">
          <div className="w-full max-w-md bg-[#18181b] border border-[#27272a] rounded-xl overflow-hidden shadow-2xl">
            <div className="p-4 border-b border-[#27272a] flex items-center justify-between bg-[#121214]/60">
              <h3 className="text-sm font-bold text-white flex items-center gap-2">
                <Plus className="w-4 h-4 text-indigo-400" />
                创建知识库
              </h3>
              <button onClick={() => setIsCreating(false)} className="text-[#71717a] hover:text-white cursor-pointer">✕</button>
            </div>
            <form onSubmit={handleCreateSave} className="p-6 space-y-4">
              <div className="space-y-1">
                <label className="text-slate-400 font-semibold uppercase tracking-wide">名称 *</label>
                <input
                  type="text" required autoFocus value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="如：产品手册"
                  className="w-full px-3 py-2 bg-[#121214] border border-[#27272a] rounded-lg text-white focus:outline-none focus:border-indigo-600 transition"
                />
              </div>
              <div className="space-y-1">
                <label className="text-slate-400 font-semibold uppercase tracking-wide">描述</label>
                <input
                  type="text" value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="这个知识库涵盖哪些内容…"
                  className="w-full px-3 py-2 bg-[#121214] border border-[#27272a] rounded-lg text-white focus:outline-none focus:border-indigo-600 transition"
                />
              </div>
              <p className="text-[10px] text-[#52525b] italic">创建后进入详情页，可上传 .md 文件。</p>
              <div className="p-4 border-t border-[#27272a] bg-[#121214] flex justify-end gap-3 -mx-6 -mb-6">
                <button type="button" onClick={() => setIsCreating(false)} className="px-4 py-2 border border-[#27272a] hover:bg-[#18181b] text-slate-400 hover:text-white rounded-lg cursor-pointer font-semibold">
                  取消
                </button>
                <button type="submit" disabled={createM.isPending} className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg font-semibold cursor-pointer disabled:opacity-60 flex items-center gap-2">
                  {createM.isPending && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  {createM.isPending ? '创建中…' : '立即创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

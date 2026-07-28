/**
 * KbDetailPage — view/edit a Knowledge Base's .md files.
 *
 * Left file tree (getFileTree) + right editor (getFileContent /
 * updateFileContent), plus upload (.md files, preserves relative paths)
 * and delete-file. Native Tailwind. Modelled on SkillDetailPage.
 */
import { useMemo, useState, type FC, type ChangeEvent } from 'react';
import {
  ArrowLeft, Folder, FileText, Loader2, Save, Undo2, Trash2, Upload,
} from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  knowledgeApi, knowledgeKeys, type KbFileTreeNode,
} from '../services/knowledge-api';
import { confirmDialog } from './ui/confirm';
import { toast } from './ui/toast';

export function KbDetailPage({
  kbId,
  kbName,
  onBack,
}: {
  kbId: string;
  kbName: string;
  onBack: () => void;
}) {
  return (
    <div className="space-y-4">
      {/* Breadcrumb / header */}
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="p-1.5 rounded-lg text-[#a1a1aa] hover:text-white hover:bg-[#27272a] transition cursor-pointer">
          <ArrowLeft className="w-4 h-4" />
        </button>
        <div>
          <div className="flex items-center gap-2 text-xs text-[#71717a]">
            <span>知识库</span><span>/</span><span className="text-white font-semibold">{kbName}</span>
          </div>
          <p className="text-[11px] text-[#52525b] mt-0.5">绑定到 Agent 后，可用 kb_glob / kb_grep / kb_read 探索</p>
        </div>
      </div>
      <KbDirectoryEditor kbId={kbId} />
    </div>
  );
}

/** File tree + editor + upload. */
function KbDirectoryEditor({ kbId }: { kbId: string }) {
  const queryClient = useQueryClient();
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const { data: treeData, isLoading } = useQuery({
    queryKey: knowledgeKeys.files(kbId),
    queryFn: () => knowledgeApi.getFileTree(kbId),
  });

  const firstFile = useMemo(() => firstLeaf(treeData?.files ?? []), [treeData]);
  if (firstFile && !selectedPath) {
    // setState in render guard — fine for one-time init.
    setTimeout(() => setSelectedPath(firstFile), 0);
  }

  const uploadM = useMutation({
    mutationFn: (files: File[]) => knowledgeApi.uploadDocuments(kbId, files),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.files(kbId) });
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.detail(kbId) });
      if (res.errors.length) {
        toast.error(`${res.created.length} 成功 / ${res.errors.length} 失败：${res.errors[0].error}`);
      } else {
        toast.success(`已上传 ${res.created.length} 个文件`);
      }
    },
    onError: (e: unknown) => toast.error(e instanceof Error ? e.message : '上传失败'),
  });

  const handleUpload = (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    if (files.length) uploadM.mutate(files);
    e.currentTarget.value = '';
  };

  return (
    <div className="flex gap-4 h-[calc(100vh-180px)] min-h-[400px]">
      {/* File tree + upload */}
      <div className="w-72 shrink-0 rounded-xl border border-[#27272a] bg-[#18181b] overflow-y-auto p-3">
        <div className="flex items-center justify-between mb-2 px-1">
          <span className="text-[11px] text-[#71717a] font-semibold">文件</span>
          <label className="flex items-center gap-1 px-2 py-1 rounded-lg text-[10px] bg-indigo-600 hover:bg-indigo-500 text-white cursor-pointer font-semibold transition">
            {uploadM.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Upload className="w-3 h-3" />}
            上传
            <input type="file" accept=".md,.markdown" multiple className="hidden" onChange={handleUpload} />
          </label>
        </div>
        {isLoading ? (
          <div className="flex items-center justify-center py-8 text-[#71717a]">
            <Loader2 className="w-4 h-4 animate-spin mr-2" /> 加载文件…
          </div>
        ) : (treeData?.files ?? []).length === 0 ? (
          <p className="text-[11px] text-[#52525b] px-1 py-4 text-center">还没有文件，点击「上传」添加 .md</p>
        ) : (
          <div className="space-y-0.5">
            {(treeData?.files ?? []).map((node) => (
              <KbTreeRow key={node.key} node={node} depth={0} selected={selectedPath} onSelect={setSelectedPath} />
            ))}
          </div>
        )}
      </div>

      {/* Editor */}
      <div className="flex-1 min-w-0">
        {selectedPath ? (
          // key=filePath forces a fresh instance per file so local/loaded state resets on switch.
          <KbFileEditor key={selectedPath} kbId={kbId} filePath={selectedPath} />
        ) : (
          <div className="flex items-center justify-center h-full text-[#52525b] text-sm border border-[#27272a] rounded-xl bg-[#18181b]">
            选择左侧文件查看内容
          </div>
        )}
      </div>
    </div>
  );
}

/** Recursively render a tree node (folders expandable, files selectable). */
const KbTreeRow: FC<{
  node: KbFileTreeNode;
  depth: number;
  selected: string | null;
  onSelect: (path: string) => void;
}> = ({ node, depth, selected, onSelect }) => {
  const [open, setOpen] = useState(true);
  const pad = { paddingLeft: `${depth * 12 + 8}px` };

  if (!node.is_leaf) {
    return (
      <div>
        <button
          onClick={() => setOpen((o) => !o)}
          style={pad}
          className="w-full flex items-center gap-1.5 py-1 text-xs text-[#a1a1aa] hover:text-white hover:bg-[#27272a] rounded transition cursor-pointer"
        >
          <Folder className="w-3.5 h-3.5 text-sky-400 shrink-0" />
          <span className="truncate font-medium">{node.title}</span>
        </button>
        {open && node.children?.map((child) => (
          <KbTreeRow key={child.key} node={child} depth={depth + 1} selected={selected} onSelect={onSelect} />
        ))}
      </div>
    );
  }

  const isSel = selected === node.key;
  return (
    <button
      onClick={() => onSelect(node.key)}
      style={pad}
      className={`w-full flex items-center gap-1.5 py-1 text-xs rounded transition cursor-pointer ${
        isSel ? 'bg-indigo-500/10 text-indigo-300' : 'text-[#a1a1aa] hover:text-white hover:bg-[#27272a]'
      }`}
    >
      <FileText className="w-3.5 h-3.5 text-[#71717a] shrink-0" />
      <span className="truncate font-mono">{node.title}</span>
    </button>
  );
};

/** Load + edit a single file, with dirty/save/delete. */
function KbFileEditor({ kbId, filePath }: { kbId: string; filePath: string }) {
  const queryClient = useQueryClient();
  const [local, setLocal] = useState<string>('');
  const [loaded, setLoaded] = useState(false);

  const { data: file, isLoading } = useQuery({
    queryKey: knowledgeKeys.fileContent(kbId, filePath),
    queryFn: () => knowledgeApi.getFileContent(kbId, filePath),
  });

  if (!loaded && file !== undefined) {
    setTimeout(() => { setLocal(file.content); setLoaded(true); }, 0);
  }
  const isDirty = loaded && local !== (file?.content ?? '');

  const saveM = useMutation({
    mutationFn: (content: string) => knowledgeApi.updateFileContent(kbId, filePath, content),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.fileContent(kbId, filePath) });
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.detail(kbId) });
    },
  });

  const deleteM = useMutation({
    mutationFn: () => knowledgeApi.deleteFile(kbId, filePath),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.files(kbId) });
      queryClient.invalidateQueries({ queryKey: knowledgeKeys.detail(kbId) });
      toast.success('文件已删除');
    },
    onError: (e: unknown) => toast.error(e instanceof Error ? e.message : '删除失败'),
  });

  const handleDelete = async () => {
    const ok = await confirmDialog({
      title: `删除文件「${filePath}」？`,
      okText: '删除',
      danger: true,
    });
    if (ok) deleteM.mutate();
  };

  return (
    <div className="h-full flex flex-col rounded-xl border border-[#27272a] bg-[#18181b] overflow-hidden">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-[#27272a] shrink-0">
        <span className="text-[11px] font-mono text-[#a1a1aa] truncate">{filePath}</span>
        <div className="flex items-center gap-2">
          {isDirty && <span className="text-[10px] text-amber-400 font-semibold">未保存</span>}
          <button
            onClick={() => file && setLocal(file.content)}
            disabled={!isDirty}
            className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-[#a1a1aa] hover:text-white hover:bg-[#27272a] disabled:opacity-40 transition cursor-pointer"
          >
            <Undo2 className="w-3 h-3" /> 撤销
          </button>
          <button
            onClick={handleDelete}
            disabled={deleteM.isPending}
            className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-rose-400 hover:bg-rose-950/30 disabled:opacity-40 transition cursor-pointer"
          >
            {deleteM.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
            删除
          </button>
          <button
            onClick={() => saveM.mutate(local)}
            disabled={!isDirty || saveM.isPending}
            className="flex items-center gap-1 px-3 py-1 rounded-lg text-[11px] bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-40 transition cursor-pointer font-semibold"
          >
            {saveM.isPending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
            保存
          </button>
        </div>
      </div>

      {/* Editor area */}
      {isLoading ? (
        <div className="flex-1 flex items-center justify-center text-[#71717a]"><Loader2 className="w-5 h-5 animate-spin mr-2" />加载…</div>
      ) : (
        <textarea
          value={local}
          onChange={(e) => setLocal(e.target.value)}
          className="flex-1 w-full p-4 bg-transparent text-[#fafafa] font-mono text-xs leading-relaxed resize-none focus:outline-none"
          spellCheck={false}
        />
      )}
      <div className="px-4 py-1.5 border-t border-[#27272a] text-[10px] text-[#52525b] shrink-0">{local.length} 字符</div>
    </div>
  );
}

/** Find the first leaf path in a tree (for auto-select). */
function firstLeaf(nodes: KbFileTreeNode[]): string | null {
  for (const n of nodes) {
    if (n.is_leaf) return n.key;
    const child = firstLeaf(n.children ?? []);
    if (child) return child;
  }
  return null;
}

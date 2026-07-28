/**
 * Knowledge Base API service — wraps backend /knowledge-bases endpoints.
 *
 * Uses the shared apiClient instance (auto auth header + 401 refresh).
 * Response fields are snake_case per backend contract.
 */
import { apiClient } from '../lib/api-client'

/* ─── Types (snake_case, matches backend schemas) ─── */

export interface KnowledgeBase {
  id: string
  name: string
  description: string
  owner_user_id: string
  status: string
  file_count: number
  total_size: number
  created_at: string
  updated_at: string
}

export interface KnowledgeBaseCreateInput {
  name: string
  description?: string
}

export interface KnowledgeBaseUpdateInput {
  name?: string
  description?: string
}

export interface KnowledgeBaseListParams {
  page?: number
  page_size?: number
  name?: string
  status?: string
}

export interface KnowledgeBaseListResponse {
  items: KnowledgeBase[]
  total: number
  page: number
  page_size: number
}

export interface KbFile {
  path: string
  content: string
  size: number
}

export interface KbFileTreeNode {
  key: string
  title: string
  is_leaf: boolean
  children?: KbFileTreeNode[]
  size: number
}

export interface KbFileTreeResponse {
  kb_id: string
  files: KbFileTreeNode[]
}

export interface KbFileUpdatePayload {
  content: string
}

export interface KbUploadResult {
  /** Relative paths written (KB files are not separate DB entities). */
  created: string[]
  errors: KbUploadError[]
}

export interface KbUploadError {
  filename: string
  error: string
}

/* ─── API methods ─── */

export const knowledgeApi = {
  /** GET /api/v1/knowledge-bases */
  async list(params: KnowledgeBaseListParams = {}): Promise<KnowledgeBaseListResponse> {
    const res = await apiClient.get<KnowledgeBaseListResponse>('/api/v1/knowledge-bases', {
      params: {
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
        ...(params.name ? { name: params.name } : {}),
        ...(params.status ? { status: params.status } : {}),
      },
    })
    return res.data
  },

  /** GET /api/v1/knowledge-bases/{id} */
  async get(kbId: string): Promise<KnowledgeBase> {
    const res = await apiClient.get<KnowledgeBase>(`/api/v1/knowledge-bases/${encodeURIComponent(kbId)}`)
    return res.data
  },

  /** POST /api/v1/knowledge-bases */
  async create(input: KnowledgeBaseCreateInput): Promise<KnowledgeBase> {
    const res = await apiClient.post<KnowledgeBase>('/api/v1/knowledge-bases', input)
    return res.data
  },

  /** PUT /api/v1/knowledge-bases/{id} */
  async update(kbId: string, input: KnowledgeBaseUpdateInput): Promise<KnowledgeBase> {
    const res = await apiClient.put<KnowledgeBase>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}`,
      input,
    )
    return res.data
  },

  /** DELETE /api/v1/knowledge-bases/{id} */
  async remove(kbId: string): Promise<void> {
    await apiClient.delete(`/api/v1/knowledge-bases/${encodeURIComponent(kbId)}`)
  },

  /** GET /api/v1/knowledge-bases/{id}/files */
  async getFileTree(kbId: string): Promise<KbFileTreeResponse> {
    const res = await apiClient.get<KbFileTreeResponse>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/files`,
    )
    return res.data
  },

  /** GET /api/v1/knowledge-bases/{id}/files/{path} */
  async getFileContent(kbId: string, filePath: string): Promise<KbFile> {
    const res = await apiClient.get<KbFile>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(filePath)}`,
    )
    return res.data
  },

  /** PUT /api/v1/knowledge-bases/{id}/files/{path} */
  async updateFileContent(kbId: string, filePath: string, content: string): Promise<KbFile> {
    const res = await apiClient.put<KbFile>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(filePath)}`,
      { content },
    )
    return res.data
  },

  /** DELETE /api/v1/knowledge-bases/{id}/files/{path} */
  async deleteFile(kbId: string, filePath: string): Promise<void> {
    await apiClient.delete(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(filePath)}`,
    )
  },

  /**
   * Upload .md file(s) into a KB.
   * POST /api/v1/knowledge-bases/{id}/documents
   *
   * Folder upload is supported: each File carries `webkitRelativePath`
   * (e.g. "notes/api.md") passed as the third append() arg so the path is
   * preserved on disk. Loose files fall back to `name`.
   */
  async uploadDocuments(kbId: string, files: File[]): Promise<KbUploadResult> {
    const formData = new FormData()
    files.forEach((f) => {
      const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name
      formData.append('files', f, rel)
    })
    const res = await apiClient.post<KbUploadResult>(
      `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/documents`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
    return res.data
  },
}

/* ─── Query key factory ─── */

export const knowledgeKeys = {
  all: ['knowledge-bases'] as const,
  lists: () => [...knowledgeKeys.all, 'list'] as const,
  list: (params: KnowledgeBaseListParams) => [...knowledgeKeys.lists(), params] as const,
  details: () => [...knowledgeKeys.all, 'detail'] as const,
  detail: (id: string) => [...knowledgeKeys.details(), id] as const,
  files: (id: string) => [...knowledgeKeys.detail(id), 'files'] as const,
  fileContent: (id: string, path: string) => [...knowledgeKeys.detail(id), 'file', path] as const,
}

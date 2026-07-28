import {
  DownloadOutlined,
  FileImageOutlined,
  FileOutlined,
  FileZipOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  App,
  Button,
  Drawer,
  Empty,
  List,
  Modal,
  Skeleton,
  Space,
  Typography,
} from 'antd'
import { useCallback, useEffect, useState } from 'react'

import {
  downloadSessionFile,
  downloadSessionZip,
  getSessionFile,
  listSessionFiles,
} from '../api/chat'
import type { SessionFile } from '../types'

interface GeneratedFilesProps {
  sessionId: string | null
  open: boolean
  onClose: () => void
  refreshKey: number
}

function fileName(file: SessionFile): string {
  return file.name || file.path
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function GeneratedFiles({
  sessionId,
  open,
  onClose,
  refreshKey,
}: GeneratedFilesProps) {
  const { message } = App.useApp()
  const [files, setFiles] = useState<SessionFile[]>([])
  const [loading, setLoading] = useState(false)
  const [preview, setPreview] = useState<{
    name: string
    kind: 'image' | 'pdf' | 'text' | 'unsupported'
    url?: string
    text?: string
  } | null>(null)

  const load = useCallback(async () => {
    if (!sessionId) {
      setFiles([])
      return
    }
    setLoading(true)
    try {
      setFiles(await listSessionFiles(sessionId))
    } catch (error: unknown) {
      void message.error(error instanceof Error ? error.message : '文件列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    void load()
  }, [load, refreshKey])

  useEffect(
    () => () => {
      if (preview?.url) URL.revokeObjectURL(preview.url)
    },
    [preview],
  )

  const openPreview = async (file: SessionFile) => {
    if (!sessionId) return
    const name = fileName(file)
    try {
      const blob = await getSessionFile(sessionId, name)
      const mime = blob.type || file.mime || ''
      if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|svg)$/i.test(name)) {
        setPreview({ name, kind: 'image', url: URL.createObjectURL(blob) })
      } else if (mime === 'application/pdf' || name.toLowerCase().endsWith('.pdf')) {
        setPreview({ name, kind: 'pdf', url: URL.createObjectURL(blob) })
      } else if (
        mime.startsWith('text/') ||
        /\.(md|txt|json|csv|html|xml|ya?ml)$/i.test(name)
      ) {
        setPreview({ name, kind: 'text', text: await blob.text() })
      } else {
        setPreview({ name, kind: 'unsupported' })
      }
    } catch {
      void message.error('文件预览失败')
    }
  }

  return (
    <>
      <Drawer
        title="会话文件"
        width={380}
        open={open}
        onClose={onClose}
        className="files-drawer"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()} />
            <Button
              icon={<FileZipOutlined />}
              disabled={!sessionId || files.length === 0}
              onClick={() => {
                if (sessionId) void downloadSessionZip(sessionId)
              }}
            >
              打包下载
            </Button>
          </Space>
        }
      >
        {loading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : files.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无会话文件" />
        ) : (
          <List
            dataSource={files}
            renderItem={(file) => {
              const name = fileName(file)
              const image = /\.(png|jpe?g|gif|webp|svg)$/i.test(name)
              return (
                <List.Item
                  actions={[
                    <Button
                      key="download"
                      type="text"
                      icon={<DownloadOutlined />}
                      onClick={() => {
                        if (sessionId) void downloadSessionFile(sessionId, name)
                      }}
                      aria-label={`下载 ${name}`}
                    />,
                  ]}
                >
                  <List.Item.Meta
                    avatar={image ? <FileImageOutlined /> : <FileOutlined />}
                    title={
                      <Button type="link" onClick={() => void openPreview(file)}>
                        {name}
                      </Button>
                    }
                    description={humanSize(file.size)}
                  />
                </List.Item>
              )
            }}
          />
        )}
      </Drawer>
      <Modal
        width="min(920px, calc(100vw - 32px))"
        title={preview?.name}
        open={Boolean(preview)}
        footer={
          preview && sessionId ? (
            <Button
              type="primary"
              icon={<DownloadOutlined />}
              onClick={() => void downloadSessionFile(sessionId, preview.name)}
            >
              下载
            </Button>
          ) : null
        }
        onCancel={() => setPreview(null)}
        destroyOnHidden
      >
        {preview?.kind === 'image' ? (
          <img className="file-preview-image" src={preview.url} alt={preview.name} />
        ) : null}
        {preview?.kind === 'pdf' ? (
          <iframe className="file-preview-pdf" src={preview.url} title={preview.name} />
        ) : null}
        {preview?.kind === 'text' ? (
          <pre className="file-preview-text">{preview.text}</pre>
        ) : null}
        {preview?.kind === 'unsupported' ? (
          <Typography.Text type="secondary">
            当前浏览器不支持预览此格式，请下载后查看。
          </Typography.Text>
        ) : null}
      </Modal>
    </>
  )
}


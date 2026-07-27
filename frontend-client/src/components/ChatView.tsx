import {
  FileOutlined,
  MenuOutlined,
  PaperClipOutlined,
  QuestionCircleOutlined,
  PlusOutlined,
} from '@ant-design/icons'
import { Attachments, Bubble, Sender } from '@ant-design/x'
import {
  Alert,
  App,
  Button,
  Empty,
  Image,
  Input,
  Modal,
  Result,
  Skeleton,
  Typography,
} from 'antd'
import type { UploadFile } from 'antd'
import { useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { useChat } from '../hooks/use-chat'
import type { AgentSummary } from '../types'
import { GeneratedFiles } from './GeneratedFiles'
import { MessageContent } from './MessageContent'

interface ChatViewProps {
  agent: AgentSummary | null
  sessionId: string | null
  onOpenNavigation: () => void
  onCreateSession: () => void
}

export function ChatView({ agent, sessionId, onOpenNavigation, onCreateSession }: ChatViewProps) {
  const { message } = App.useApp()
  const [input, setInput] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [filesOpen, setFilesOpen] = useState(false)
  const [filesRefreshKey, setFilesRefreshKey] = useState(0)
  const [clarificationAnswer, setClarificationAnswer] = useState('')
  const [pendingPreview, setPendingPreview] = useState<{
    name: string
    url: string
  } | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const {
    messages,
    loading,
    running,
    hitl,
    loadError,
    send,
    cancel,
    answerClarification,
  } = useChat(agent?.id ?? null, sessionId, () =>
    setFilesRefreshKey((value) => value + 1),
  )

  const attachmentItems = useMemo<UploadFile[]>(
    () =>
      files.map((file, index) => ({
        uid: `${index}:${file.name}:${file.lastModified}`,
        name: file.name,
        size: file.size,
        type: file.type,
        status: 'done',
      })),
    [files],
  )

  const addFiles = (next: File[]) => {
    setFiles((current) => {
      const merged = [...current]
      for (const file of next) {
        if (
          !merged.some(
            (item) =>
              item.name === file.name &&
              item.size === file.size &&
              item.lastModified === file.lastModified,
          )
        ) {
          merged.push(file)
        }
      }
      if (merged.length > 8) void message.warning('单次最多上传 8 个文件')
      return merged.slice(0, 8)
    })
  }

  const submit = (value: string) => {
    void send(value, files)
    setInput('')
    setFiles([])
  }

  const submitClarification = (answer: string) => {
    const value = answer.trim()
    if (!value) return
    void answerClarification(value)
    setClarificationAnswer('')
  }

  if (!agent) {
    return (
      <main className="chat-view">
        <header className="chat-header">
          <Button
            className="mobile-nav-button"
            type="text"
            icon={<MenuOutlined />}
            onClick={onOpenNavigation}
          />
        </header>
        <Result
          status="info"
          title="暂无可用 Agent"
          subTitle="请联系管理员为当前公司分配可调用的 Agent。"
        />
      </main>
    )
  }

  return (
    <main className="chat-view">
      <header className="chat-header">
        <Button
          className="mobile-nav-button"
          type="text"
          icon={<MenuOutlined />}
          onClick={onOpenNavigation}
          aria-label="打开对话列表"
        />
        <div className="chat-agent-title">
          <strong>{agent.name}</strong>
          <Typography.Text type="secondary" ellipsis>
            {agent.description || '智能 Agent'}
          </Typography.Text>
        </div>
        <Button
          type="text"
          icon={<FileOutlined />}
          disabled={!sessionId}
          onClick={() => setFilesOpen(true)}
        >
          <span className="desktop-only-label">会话文件</span>
        </Button>
      </header>

      {!sessionId ? (
        <div className="chat-empty">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有对话，开始创建一个吧"
          >
            <Button type="primary" icon={<PlusOutlined />} onClick={onCreateSession}>
              快速创建对话
            </Button>
          </Empty>
        </div>
      ) : (
        <>
          <section className="message-viewport" aria-live="polite">
            {loading ? (
              <div className="message-loading">
                <Skeleton active avatar paragraph={{ rows: 3 }} />
                <Skeleton active paragraph={{ rows: 2 }} />
              </div>
            ) : loadError ? (
              <Alert type="error" showIcon message="历史消息加载失败" description={loadError} />
            ) : messages.length === 0 ? (
              <div className="welcome-state">
                <div className="welcome-mark">{agent.name.slice(0, 1)}</div>
                {agent.welcomeMessage ? (
                  <div className="welcome-message">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {agent.welcomeMessage}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <>
                    <Typography.Title level={2}>和 {agent.name} 开始对话</Typography.Title>
                    <Typography.Paragraph type="secondary">
                      可以直接提问，也可以上传图片、文档或数据文件。
                    </Typography.Paragraph>
                  </>
                )}
              </div>
            ) : (
              <Bubble.List
                autoScroll
                items={messages.map((chatMessage) => ({
                  key: chatMessage.id,
                  role: chatMessage.role === 'user' ? 'user' : 'ai',
                  status:
                    chatMessage.status === 'loading'
                      ? 'updating'
                      : chatMessage.status,
                  content: (
                    <MessageContent message={chatMessage} sessionId={sessionId} />
                  ),
                  streaming: chatMessage.status === 'loading',
                }))}
                role={{
                  ai: {
                    placement: 'start',
                    variant: 'borderless',
                  },
                  user: {
                    placement: 'end',
                    variant: 'filled',
                  },
                }}
              />
            )}
          </section>

          <footer className="composer-dock">
            {hitl ? (
              <Alert
                className="hitl-card"
                type="warning"
                showIcon
                icon={<QuestionCircleOutlined />}
                message={
                  hitl.clarificationType === 'risk_confirmation'
                    ? '操作确认'
                    : 'Agent 需要补充信息'
                }
                description={
                  <div className="clarification-content">
                    <Typography.Text>{hitl.question}</Typography.Text>
                    {hitl.context ? (
                      <Typography.Text type="secondary">{hitl.context}</Typography.Text>
                    ) : null}
                    {hitl.options.length > 0 ? (
                      <div className="clarification-options">
                        {hitl.options.map((option) => (
                          <Button
                            key={option}
                            onClick={() => submitClarification(option)}
                          >
                            {option}
                          </Button>
                        ))}
                      </div>
                    ) : null}
                    {hitl.clarificationType === 'risk_confirmation' ? (
                      <div className="clarification-options">
                        <Button onClick={() => submitClarification('取消')}>取消</Button>
                        <Button
                          danger
                          type="primary"
                          onClick={() => submitClarification('确认')}
                        >
                          确认执行
                        </Button>
                      </div>
                    ) : null}
                    <div className="clarification-input">
                      <Input
                        value={clarificationAnswer}
                        onChange={(event) => setClarificationAnswer(event.target.value)}
                        onPressEnter={() => submitClarification(clarificationAnswer)}
                        placeholder="输入你的回答"
                        disabled={running}
                      />
                      <Button
                        type="primary"
                        disabled={!clarificationAnswer.trim() || running}
                        onClick={() => submitClarification(clarificationAnswer)}
                      >
                        发送
                      </Button>
                    </div>
                  </div>
                }
              />
            ) : null}
            {files.length > 0 ? (
              <Attachments
                className="composer-attachments"
                items={attachmentItems}
                overflow="scrollX"
                onPreview={(item) => {
                  const selected = files.find(
                    (file, index) =>
                      `${index}:${file.name}:${file.lastModified}` === item.uid,
                  )
                  if (!selected || !selected.type.startsWith('image/')) {
                    void message.info('该文件将在发送后支持下载')
                    return
                  }
                  setPendingPreview({
                    name: selected.name,
                    url: URL.createObjectURL(selected),
                  })
                }}
                onRemove={(file) => {
                  setFiles((current) =>
                    current.filter(
                      (item, index) =>
                        `${index}:${item.name}:${item.lastModified}` !== file.uid,
                    ),
                  )
                }}
              />
            ) : null}
            {agent.recommendedItems && agent.recommendedItems.length > 0 ? (
              <div className="composer-quick-actions">
                {agent.recommendedItems.map((item, index) => (
                  <Button
                    key={`${index}:${item.label}`}
                    className="quick-action"
                    disabled={running || Boolean(hitl)}
                    onClick={() => submit(item.prompt || item.label)}
                  >
                    {item.label}
                  </Button>
                ))}
              </div>
            ) : null}
            <Sender
              value={input}
              onChange={setInput}
              onSubmit={submit}
              onCancel={cancel}
              onPasteFile={(pasted) => addFiles(Array.from(pasted))}
              loading={running}
              disabled={Boolean(hitl)}
              placeholder={hitl ? '请先处理待确认操作' : '输入消息，Enter 发送'}
              autoSize={{ minRows: 1, maxRows: 6 }}
              prefix={
                <Button
                  type="text"
                  icon={<PaperClipOutlined />}
                  onClick={() => fileInputRef.current?.click()}
                  aria-label="上传附件"
                  disabled={running || Boolean(hitl)}
                />
              }
            />
            <input
              ref={fileInputRef}
              className="hidden-file-input"
              type="file"
              multiple
              onChange={(event) => {
                addFiles(Array.from(event.target.files ?? []))
                event.currentTarget.value = ''
              }}
            />
            <Typography.Text className="composer-note" type="secondary">
              Agent 输出可能有误，请核对关键结果。写操作执行前会再次确认。
            </Typography.Text>
          </footer>
        </>
      )}

      <GeneratedFiles
        sessionId={sessionId}
        open={filesOpen}
        onClose={() => setFilesOpen(false)}
        refreshKey={filesRefreshKey}
      />
      <Modal
        title={pendingPreview?.name}
        open={Boolean(pendingPreview)}
        footer={null}
        onCancel={() => {
          if (pendingPreview?.url) URL.revokeObjectURL(pendingPreview.url)
          setPendingPreview(null)
        }}
        destroyOnHidden
      >
        {pendingPreview ? (
          <Image
            className="file-preview-image"
            src={pendingPreview.url}
            alt={pendingPreview.name}
            preview={false}
          />
        ) : null}
      </Modal>
    </main>
  )
}

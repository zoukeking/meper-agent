import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  DatabaseOutlined,
  FileOutlined,
  ToolOutlined,
} from '@ant-design/icons'
import { Mermaid } from '@ant-design/x'
import { App, Button, Collapse, Image, Tag, Tooltip, Typography } from 'antd'
import { isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { downloadSessionFile, downloadUploadedFile } from '../api/chat'
import type { AttachmentView, ChatMessage, ToolRun } from '../types'
import { ChartBlock } from './ChartBlock'
import { WorkflowTaskCard, parseTaskCreated } from './WorkflowTaskCard'

function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ href, children: label }) => (
          <a href={href} target="_blank" rel="noreferrer">
            {label}
          </a>
        ),
        code: ({ className, children: codeChildren, ...props }) => {
          const language = /language-([^\s]+)/.exec(className ?? '')?.[1]
          const source = String(codeChildren).replace(/\n$/, '')
          if (language === 'echarts' || language === 'chart') {
            return <ChartBlock source={source} />
          }
          if (language === 'mermaid') {
            return <Mermaid>{source}</Mermaid>
          }
          return (
            <code className={className} {...props}>
              {codeChildren}
            </code>
          )
        },
        pre: ({ children: preChildren, ...props }) => (
          isValidElement(preChildren) &&
          (preChildren.type === ChartBlock || preChildren.type === Mermaid) ? (
            preChildren
          ) : (
            <pre {...props}>{preChildren}</pre>
          )
        ),
        table: ({ children: tableChildren, ...props }) => (
          <div className="markdown-table-wrap">
            <table {...props}>{tableChildren}</table>
          </div>
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  )
}

function promotedCharts(tools: ToolRun[]): string[] {
  const seen = new Set<string>()
  const charts: string[] = []
  for (const tool of tools) {
    if (tool.name !== 'render_chart' || !tool.result) continue
    const match = tool.result.match(/```(?:echarts|chart)\s*\n([\s\S]*?)\n```/i)
    const source = match?.[1]?.trim()
    if (!source || seen.has(source)) continue
    seen.add(source)
    charts.push(source)
  }
  return charts
}

function statusIcon(tool: ToolRun): ReactNode {
  if (tool.status === 'running') return <ClockCircleOutlined spin />
  if (tool.status === 'error') return <CloseCircleOutlined />
  return <CheckCircleOutlined />
}

function ToolResult({ tool }: { tool: ToolRun }) {
  // dispatch_workflow：解析 task_created → 内嵌可展开 task 卡片（自带详情/轮询/操作）。
  // 解析失败则落到下方通用工具结果渲染。
  if (tool.name === 'dispatch_workflow') {
    const created = parseTaskCreated(tool.result)
    if (created) return <WorkflowTaskCard created={created} />
  }
  const isKnowledge = tool.name === 'kb_retrieve' || tool.name === 'search_kb'
  const title = isKnowledge ? '知识库检索' : tool.name
  return (
    <Collapse
      className={`tool-run tool-run-${tool.status}`}
      size="small"
      ghost
      items={[
        {
          key: tool.id,
          label: (
            <div className="tool-title">
              {statusIcon(tool)}
              {isKnowledge ? <DatabaseOutlined /> : <ToolOutlined />}
              <span>{title}</span>
              {tool.auto ? <Tag>自动召回</Tag> : null}
            </div>
          ),
          children: (
            <div className="tool-details">
              {tool.args ? (
                <section>
                  <Typography.Text type="secondary">请求参数</Typography.Text>
                  <pre>{tool.args}</pre>
                </section>
              ) : null}
              {tool.result ? (
                <section>
                  <Typography.Text type="secondary">执行结果</Typography.Text>
                  <div className="tool-result-body">
                    <Markdown>{tool.result}</Markdown>
                  </div>
                </section>
              ) : tool.status === 'running' ? (
                <Typography.Text type="secondary">正在执行...</Typography.Text>
              ) : null}
            </div>
          ),
        },
      ]}
    />
  )
}

function AttachmentItem({
  attachment,
  sessionId,
}: {
  attachment: AttachmentView
  sessionId: string
}) {
  const { message } = App.useApp()
  if (attachment.kind === 'image' && attachment.url) {
    return (
      <Image
        className="message-image"
        src={attachment.url}
        alt={attachment.name}
        preview
      />
    )
  }
  return (
    <Button
      className="message-file"
      icon={<FileOutlined />}
      onClick={() => {
        const download =
          attachment.source === 'upload'
            ? downloadUploadedFile(attachment.id, attachment.name)
            : downloadSessionFile(sessionId, attachment.name)
        void download.catch(() =>
          message.error('文件下载失败'),
        )
      }}
    >
      {attachment.name}
    </Button>
  )
}

interface MessageContentProps {
  message: ChatMessage
  sessionId: string
}

export function MessageContent({ message, sessionId }: MessageContentProps) {
  const { message: toast } = App.useApp()
  const charts = [...promotedCharts(message.tools), ...message.charts]
  return (
    <div className={`message-content message-content-${message.role}`}>
      {message.reasoning ? (
        <Collapse
          className="reasoning-panel"
          ghost
          size="small"
          items={[
            {
              key: 'reasoning',
              label: message.status === 'loading' ? '正在思考' : '思考过程',
              children: <Markdown>{message.reasoning}</Markdown>,
            },
          ]}
        />
      ) : null}
      {message.tools.map((tool) => (
        <ToolResult key={tool.id} tool={tool} />
      ))}
      {message.attachments.length > 0 ? (
        <div className="message-attachments">
          {message.attachments.map((attachment) => (
            <AttachmentItem
              key={attachment.id}
              attachment={attachment}
              sessionId={sessionId}
            />
          ))}
        </div>
      ) : null}
      {charts.map((source, index) => (
        <ChartBlock key={`chart:${index}:${source.slice(0, 32)}`} source={source} />
      ))}
      {message.text ? <Markdown>{message.text}</Markdown> : null}
      {message.status === 'loading' && !message.text && !message.reasoning ? (
        <Typography.Text type="secondary">正在响应...</Typography.Text>
      ) : null}
      {message.error ? (
        <Typography.Text type="danger">{message.error}</Typography.Text>
      ) : null}
      {message.role === 'assistant' && message.text ? (
        <div className="message-actions">
          <Tooltip title="复制回答">
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              onClick={() => {
                void navigator.clipboard.writeText(message.text)
                void toast.success('已复制')
              }}
              aria-label="复制回答"
            />
          </Tooltip>
        </div>
      ) : null}
    </div>
  )
}

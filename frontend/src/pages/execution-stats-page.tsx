/**
 * Execution statistics page — unified stats + detail log.
 *
 * Combines a cross-channel statistics overview (internal / api_key / im)
 * with a paginated execution-log detail table. All data comes from the
 * `execution_logs` collection (independent of session lifecycle), so
 * deleting a session does not affect these stats.
 */
import { useMemo, useState, Component, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Card, DatePicker, Segmented, Spin, Empty, Table, Statistic, Tag, Button,
} from 'antd'
import { Column, Pie } from '@ant-design/charts'
import dayjs, { type Dayjs } from 'dayjs'
import {
  MessageOutlined, ThunderboltOutlined, CheckCircleOutlined,
} from '@ant-design/icons'
import {
  statsApi, type ChannelStats, type ExecutionStats, type ExecutionLogItem,
} from '../services/stats-api'

const { RangePicker } = DatePicker

type ChannelKey = 'internal' | 'api_key' | 'im'

const CHANNEL_META: Record<ChannelKey, { label: string; color: string }> = {
  internal: { label: '内部用户', color: '#2563EB' },
  api_key: { label: 'API Key', color: '#10B981' },
  im: { label: 'IM 渠道', color: '#F59E0B' },
}

/** 图表错误边界：单个图表渲染失败时不让整个页面白屏。 */
class ChartErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  render() {
    if (this.state.hasError) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="图表渲染失败" />
    }
    return this.props.children
  }
}

function buildParams(
  mode: 'range' | 'day',
  range: [Dayjs, Dayjs] | null,
  day: Dayjs | null,
): { start?: string; end?: string; date?: string } {
  if (mode === 'day' && day) {
    return { date: day.format('YYYY-MM-DD') }
  }
  if (mode === 'range' && range) {
    return {
      start: range[0].startOf('day').toISOString(),
      end: range[1].add(1, 'day').startOf('day').toISOString(),
    }
  }
  return {}
}

export default function ExecutionStatsPage() {
  const [mode, setMode] = useState<'range' | 'day'>('range')
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>([
    dayjs().subtract(29, 'day'), dayjs(),
  ])
  const [day, setDay] = useState<Dayjs | null>(dayjs())

  const [committedParams, setCommittedParams] = useState<{ start?: string; end?: string; date?: string }>(
    () => buildParams('range', [dayjs().subtract(29, 'day'), dayjs()], null),
  )
  const params = useMemo(() => buildParams(mode, range, day), [mode, range, day])

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['execution-stats', committedParams],
    queryFn: async () => {
      const res = await statsApi.getExecutionStats(committedParams)
      return res.data
    },
  })

  const handleQuery = () => setCommittedParams(params)

  const errorDetail = useMemo(() => {
    if (!isError || !error) return null
    const anyErr = error as { response?: { status?: number; data?: { error?: { message?: string } } }; message?: string }
    const status = anyErr.response?.status
    const backendMsg = anyErr.response?.data?.error?.message
    return status ? `HTTP ${status}${backendMsg ? `：${backendMsg}` : ''}` : (anyErr.message || '未知错误')
  }, [isError, error])

  return (
    <div className="min-h-full bg-[#F8FAFC] p-6">
      <div className="max-w-[1400px] mx-auto space-y-6">
        {/* Header + filters */}
        <div className="flex items-center justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-xl font-semibold text-[#0F172A]">执行统计</h1>
            <p className="text-sm text-[#64748B] mt-1">
              按接入通道统计 Agent 执行情况（数据独立于会话，删会话不影响）
            </p>
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <Segmented
              value={mode}
              onChange={(v) => setMode(v as 'range' | 'day')}
              options={[
                { label: '指定某天', value: 'day' },
                { label: '时间范围', value: 'range' },
              ]}
            />
            {mode === 'day' ? (
              <DatePicker value={day} onChange={(d) => setDay(d)} allowClear={false} />
            ) : (
              <RangePicker
                value={range as never}
                onChange={(r) => setRange(r as [Dayjs, Dayjs] | null)}
              />
            )}
            <Button type="primary" onClick={handleQuery} loading={isFetching}>查询</Button>
          </div>
        </div>

        {isError ? (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4">
            <p className="text-red-600 font-medium">数据加载失败</p>
            <p className="text-sm text-red-500 mt-1">{errorDetail}</p>
            <Button size="small" className="mt-2" onClick={() => refetch()}>重试</Button>
          </div>
        ) : isLoading || isFetching ? (
          <div className="flex justify-center py-20"><Spin size="large" /></div>
        ) : !data ? (
          <Empty description="请点击「查询」加载数据" />
        ) : (
          <>
            <StatsContent data={data} />
            <DetailTable params={committedParams} />
          </>
        )}
      </div>
    </div>
  )
}

function StatsContent({ data }: { data: ExecutionStats }) {
  if (!data?.channels) {
    return <Empty description="暂无统计数据" />
  }
  const totals = data.totals
  // 只显示有数据的通道（calls>0），避免 IM 空数据撑出空柱。
  const entries = (Object.entries(data.channels) as [ChannelKey, ChannelStats][])
    .filter(([, s]) => s.calls > 0)

  const cards = [
    { icon: <MessageOutlined />, label: '总调用次数', value: totals.calls, color: '#2563EB' },
    { icon: <ThunderboltOutlined />, label: 'Token 消耗', value: totals.tokens, color: '#7C3AED' },
    { icon: <ThunderboltOutlined />, label: 'LLM 调用', value: totals.llm_calls, color: '#0891B2' },
    { icon: <CheckCircleOutlined />, label: '成功率', value: totals.success_rate, suffix: '%', color: '#10B981' },
  ]

  const tokenData = entries.map(([key, s]) => ({
    channel: CHANNEL_META[key].label, tokens: s.tokens,
  }))
  const successData = entries.flatMap(([key, s]) => [
    { channel: `${CHANNEL_META[key].label} 成功`, value: s.success },
    { channel: `${CHANNEL_META[key].label} 失败`, value: s.failed },
  ]).filter((d) => d.value > 0)
  const callsData = entries.map(([key, s]) => ({
    channel: CHANNEL_META[key].label, value: s.calls,
  }))

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {cards.map((c) => (
          <Card key={c.label} className="!rounded-xl">
            <Statistic
              title={<span className="text-sm text-[#64748B]">{c.label}</span>}
              value={c.value}
              suffix={c.suffix}
              prefix={<span style={{ color: c.color }}>{c.icon}</span>}
              valueStyle={{ color: '#0F172A', fontSize: 28 }}
            />
          </Card>
        ))}
      </div>

      {entries.length === 0 ? (
        <Card className="!rounded-xl"><Empty description="所选时间范围内无执行记录" /></Card>
      ) : (
        <>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card title="Token 消耗对比（按通道）" className="!rounded-xl">
              <ChartErrorBoundary>
                <Column
                  data={tokenData}
                  xField="channel"
                  yField="tokens"
                  height={280}
                  colorField="channel"
                  style={{ maxWidth: 60 }}
                />
              </ChartErrorBoundary>
            </Card>
            <Card title="成功 / 失败分布" className="!rounded-xl">
              {successData.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ) : (
                <ChartErrorBoundary>
                  <Pie data={successData} angleField="value" colorField="channel" height={280} />
                </ChartErrorBoundary>
              )}
            </Card>
          </div>

          <Card title="调用次数（按通道）" className="!rounded-xl">
            <ChartErrorBoundary>
              <Column
                data={callsData}
                xField="channel"
                yField="value"
                height={280}
                colorField="channel"
                style={{ maxWidth: 60 }}
              />
            </ChartErrorBoundary>
          </Card>

          {/* Task 维度 */}
          {data.tasks && (
            <Card title="工作流执行（Task 维度）" className="!rounded-xl">
              <ChartErrorBoundary>
                <Column
                  data={[
                    { type: '内部触发', value: data.tasks.internal.tasks },
                    { type: 'API Key 触发', value: data.tasks.api_key.tasks },
                    { type: 'Agent 触发', value: data.tasks.agent_triggered.tasks },
                    { type: '定时触发', value: data.tasks.scheduled.tasks },
                  ].filter((d) => d.value > 0)}
                  xField="type"
                  yField="value"
                  height={240}
                  colorField="type"
                  style={{ maxWidth: 60 }}
                />
              </ChartErrorBoundary>
            </Card>
          )}
        </>
      )}
    </div>
  )
}

/** Paginated execution-log detail table. */
function DetailTable({ params }: { params: { start?: string; end?: string; date?: string } }) {
  const [sourceFilter, setSourceFilter] = useState<string | undefined>(undefined)
  const [page, setPage] = useState(1)

  const { data, isLoading } = useQuery({
    queryKey: ['execution-logs', params, sourceFilter, page],
    queryFn: async () => {
      const res = await statsApi.listExecutionLogs({
        ...params,
        source: sourceFilter,
        page,
        page_size: 15,
      })
      return res.data
    },
  })

  // 切换通道筛选时重置到第一页（避免 useEffect 级联渲染）。
  const handleSourceChange = (v: string | number) => {
    setSourceFilter(v === 'all' ? undefined : String(v))
    setPage(1)
  }

  const columns = [
    {
      title: '通道', dataIndex: 'source', key: 'source', width: 100,
      render: (v: string) => {
        const meta = CHANNEL_META[v as ChannelKey]
        return meta ? <Tag color={meta.color}>{meta.label}</Tag> : <Tag>{v}</Tag>
      },
    },
    {
      title: '执行者', dataIndex: 'caller_name', key: 'caller_name', width: 140, ellipsis: true,
      render: (v: string) => v || '-',
    },
    { title: 'Agent', dataIndex: 'agent_id', key: 'agent_id', width: 180, ellipsis: true,
      render: (v: string) => v || '-' },
    { title: '状态', dataIndex: 'status', key: 'status', width: 80,
      render: (v: string) => <Tag color={v === 'success' ? 'green' : 'red'}>{v === 'success' ? '成功' : '失败'}</Tag> },
    { title: 'Token', dataIndex: 'total_tokens', key: 'total_tokens', width: 100,
      render: (v: number) => v ? v.toLocaleString() : '-' },
    { title: '耗时(ms)', dataIndex: 'latency_ms', key: 'latency_ms', width: 100,
      render: (v: number) => v || '-' },
    { title: '时间', dataIndex: 'timestamp', key: 'timestamp', width: 180,
      render: (v: string) => v ? dayjs(v).format('MM-DD HH:mm:ss') : '-' },
  ]

  return (
    <Card
      title="执行明细"
      className="!rounded-xl"
      extra={
        <Segmented
          value={sourceFilter || 'all'}
          onChange={handleSourceChange}
          options={[
            { label: '全部', value: 'all' },
            { label: '内部', value: 'internal' },
            { label: 'API Key', value: 'api_key' },
            { label: 'IM', value: 'im' },
          ]}
        />
      }
    >
      <Table
        dataSource={data?.items || []}
        columns={columns}
        rowKey={(r: ExecutionLogItem) => r.request_id || JSON.stringify(r)}
        loading={isLoading}
        size="middle"
        pagination={{
          current: page,
          pageSize: 15,
          total: data?.total || 0,
          onChange: setPage,
          showTotal: (t) => `共 ${t} 条`,
        }}
      />
    </Card>
  )
}

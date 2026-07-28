import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import ReactECharts from 'echarts-for-react'
import { Alert, Skeleton } from 'antd'
import { useMemo } from 'react'

import { useAuthStore } from '../store/auth'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

interface ChartBlockProps {
  source: string
}

function completeJson(source: string): boolean {
  let depth = 0
  let quoted = false
  let escaped = false
  for (const character of source) {
    if (escaped) {
      escaped = false
      continue
    }
    if (character === '\\' && quoted) {
      escaped = true
      continue
    }
    if (character === '"') quoted = !quoted
    if (quoted) continue
    if (character === '{' || character === '[') depth += 1
    if (character === '}' || character === ']') depth -= 1
  }
  return depth === 0 && !quoted
}

export function ChartBlock({ source }: ChartBlockProps) {
  const theme = useAuthStore((state) => state.theme)
  const result = useMemo(() => {
    const raw = source.trim()
    if (!raw || !completeJson(raw)) return { state: 'loading' as const }
    if (new Blob([raw]).size > 1024 * 1024) {
      return { state: 'error' as const, message: '图表数据超过 1MB，已停止渲染' }
    }
    try {
      const option = JSON.parse(raw) as Record<string, unknown>
      if (!Array.isArray(option.series) || option.series.length === 0) {
        return { state: 'error' as const, message: '图表数据缺少 series' }
      }
      return { state: 'ready' as const, option }
    } catch {
      return { state: 'error' as const, message: '图表数据不是合法 JSON' }
    }
  }, [source])

  if (result.state === 'loading') {
    return <Skeleton.Node className="chart-skeleton" active />
  }
  if (result.state === 'error') {
    return <Alert type="warning" showIcon message={result.message} />
  }
  return (
    <div className="chart-block">
      <ReactECharts
        echarts={echarts}
        option={result.option}
        theme={theme === 'dark' ? 'dark' : undefined}
        notMerge
        lazyUpdate
        style={{ width: '100%', height: 320 }}
      />
    </div>
  )
}


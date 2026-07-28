/**
 * TanStack Query client instance.
 */
import { QueryClient } from '@tanstack/react-query'
import { message } from 'antd'

/** 从未知错误对象提取用户可读的消息。 */
export function extractErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return (err as { message: string }).message
  }
  return '操作失败'
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000, // 30 seconds
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      // 全局兜底：所有 mutation 失败自动 toast，避免遗漏 onError 导致静默。
      // 需要静默的调用点显式覆盖 onError: () => {}。
      onError: (err: unknown) => {
        message.error(extractErrorMessage(err))
      },
    },
  },
})

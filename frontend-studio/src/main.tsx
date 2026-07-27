import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import App from './App.tsx';
import {AuthInitializer} from './components/AuthInitializer';
import {toast} from './components/ui/toast';
import './index.css';
import '@xyflow/react/dist/style.css';

/** 从未知错误对象提取用户可读的消息。 */
function extractErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return (err as { message: string }).message;
  }
  return '操作失败';
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
    mutations: {
      // 全局兜底：所有 mutation 失败自动 toast，避免遗漏 onError 导致静默。
      onError: (err: unknown) => {
        toast.error(extractErrorMessage(err), {duration: 0});
      },
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthInitializer>
        <App />
      </AuthInitializer>
    </QueryClientProvider>
  </StrictMode>,
);


/**
 * Axios HTTP client with interceptors.
 *
 * - Adds X-Request-ID header for traceability (matches backend middleware)
 * - Adds Authorization header from auth store
 * - On 401 TOKEN_EXPIRED: silently refreshes the token and replays the request
 *   (single-flight to avoid concurrent refresh storms)
 */
import axios, {
  type AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

import { ENV } from '../config/env'
import { generateRequestId } from '../lib/request-id'
import { REFRESH_TOKEN_KEY, useAuthStore } from '../stores/auth-store'
import { authApi } from './auth-api'

// Allow custom markers on request config (e.g. _skipAuthRefresh to bypass the
// 401 auto-refresh interceptor for the refresh request itself).
declare module 'axios' {
  interface AxiosRequestConfig {
    _skipAuthRefresh?: boolean
  }
}

const apiClient: AxiosInstance = axios.create({
  baseURL: ENV.API_BASE_URL,
  timeout: 30_000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Request interceptor: inject trace IDs + auth token
apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  config.headers.set('X-Request-ID', generateRequestId())

  const accessToken = useAuthStore.getState().accessToken
  if (accessToken) {
    config.headers.set('Authorization', `Bearer ${accessToken}`)
  }
  return config
})

// ---------------------------------------------------------------------------
// Response interceptor: normalize error envelopes + handle silent refresh on 401
// ---------------------------------------------------------------------------

let isRefreshing = false
let refreshPromise: Promise<string> | null = null

interface ErrorEnvelope {
  error?: { code?: string; message?: string }
}

/**
 * Acquire a single in-flight refresh promise. Concurrent callers reuse it.
 * Returns the new access token or throws on failure.
 */
async function refreshAccessToken(): Promise<string> {
  if (isRefreshing && refreshPromise) {
    return refreshPromise
  }

  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
  if (!refreshToken) {
    throw new Error('No refresh token available')
  }

  isRefreshing = true
  refreshPromise = authApi
    .refresh(refreshToken, { _skipAuthRefresh: true })
    .then((res) => {
      const { access_token, refresh_token, user } = res.data
      localStorage.setItem(REFRESH_TOKEN_KEY, refresh_token)
      useAuthStore.getState().setAccessToken(access_token)
      // Sync permissions from refresh response if user info is included
      if (user?.permissions) {
        useAuthStore.getState().setUserPermissions(user.permissions)
      }
      return access_token
    })
    .finally(() => {
      isRefreshing = false
      refreshPromise = null
    })

  return refreshPromise
}

/**
 * Redirect to /login, clearing auth state. Used when refresh fails.
 */
function redirectToLogin(): void {
  useAuthStore.getState().clearAuth()
  if (window.location.pathname !== '/login') {
    const redirect = encodeURIComponent(window.location.pathname + window.location.search)
    window.location.href = `/login?redirect=${redirect}`
  }
}

apiClient.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error: AxiosError<ErrorEnvelope>) => {
    const originalRequest = error.config as (AxiosRequestConfig & { _retried?: boolean; _skipAuthRefresh?: boolean }) | undefined

    const status = error.response?.status
    const errorCode = error.response?.data?.error?.code

    // Only attempt refresh once per request, and only on 401 TOKEN_EXPIRED.
    // Skip refresh for the refresh request itself (avoids infinite recursion
    // when the refresh token is also expired — redeploys invalidate old tokens).
    if (
      status === 401 &&
      errorCode === 'TOKEN_EXPIRED' &&
      originalRequest &&
      !originalRequest._retried &&
      !originalRequest._skipAuthRefresh
    ) {
      originalRequest._retried = true
      try {
        const newToken = await refreshAccessToken()
        // Replay the original request with the fresh token
        const headers = new AxiosHeaders(originalRequest.headers as AxiosHeaders)
        headers.set('Authorization', `Bearer ${newToken}`)
        return apiClient.request({ ...originalRequest, headers })
      } catch {
        redirectToLogin()
        return Promise.reject(
          normalizeError({ code: 'TOKEN_EXPIRED', message: '登录已过期，请重新登录' }),
        )
      }
    }

    return Promise.reject(normalizeError(error))
  },
)

import { AxiosHeaders } from 'axios'

/**
 * Normalize backend error envelopes into a uniform ApiError-like object.
 * Preserves the original `code` so callers can branch on it.
 */
export interface NormalizedApiError {
  code?: string
  message: string
  statusCode?: number
}

export function normalizeError(
  source: AxiosError<ErrorEnvelope> | { code: string; message: string },
): NormalizedApiError {
  if ('isAxiosError' in source && source.isAxiosError) {
    const envelope = source.response?.data?.error
    return {
      code: envelope?.code,
      message: envelope?.message ?? source.message ?? '请求失败',
      statusCode: source.response?.status,
    }
  }
  return source as { code: string; message: string }
}

export { apiClient }

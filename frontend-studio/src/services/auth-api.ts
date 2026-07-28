/**
 * Auth API service — login, refresh, logout, change password.
 *
 * All requests go through the shared apiClient which handles
 * Authorization headers and 401 refresh logic.
 */
import type { AxiosRequestConfig } from 'axios'
import { apiClient } from '../lib/api-client'
import type { TokenResponse } from './types'

export interface ChangePasswordPayload {
  current_password: string
  new_password: string
}

export const authApi = {
  login: (username: string, password: string) =>
    apiClient.post<TokenResponse>('/api/v1/auth/login', {
      username,
      password,
    }),

  refresh: (refreshToken: string, config?: AxiosRequestConfig) =>
    apiClient.post<TokenResponse>(
      '/api/v1/auth/refresh',
      {
        refresh_token: refreshToken,
      },
      config,
    ),

  logout: (refreshToken: string) =>
    apiClient.post('/api/v1/auth/logout', {
      refresh_token: refreshToken,
    }),

  /**
   * Change the authenticated user's password.
   * POST /api/v1/auth/change-password
   * 成功后后端会失效所有 refresh token，前端需引导重新登录。
   */
  changePassword: (data: ChangePasswordPayload) =>
    apiClient.post<{ message: string }>('/api/v1/auth/change-password', data),
}

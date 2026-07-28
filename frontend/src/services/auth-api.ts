/**
 * Auth API service — login, refresh, logout.
 *
 * All requests go through the shared apiClient which handles
 * Authorization headers and 401 refresh logic.
 */
import type { AxiosRequestConfig } from 'axios'
import { apiClient } from './api-client'

export interface UserInfo {
  id: string
  username: string
  role: string
  permissions: string[]
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user?: UserInfo
}

export const authApi = {
  login: (username: string, password: string) =>
    apiClient.post<TokenResponse>('/api/v1/auth/login', {
      username,
      password,
    }),

  refresh: (refreshToken: string, config?: AxiosRequestConfig) =>
    apiClient.post<TokenResponse>('/api/v1/auth/refresh', {
      refresh_token: refreshToken,
    }, config),

  logout: (refreshToken: string) =>
    apiClient.post('/api/v1/auth/logout', {
      refresh_token: refreshToken,
    }),
}

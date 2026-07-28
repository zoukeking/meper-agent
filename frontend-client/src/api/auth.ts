import { apiRequest } from './client'
import { useAuthStore } from '../store/auth'
import type { TokenResponse } from '../types'

export async function login(
  username: string,
  password: string,
): Promise<void> {
  const bundle = await apiRequest<TokenResponse>('/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({
      username,
      password,
    }),
  })
  useAuthStore.getState().setSession(bundle)
}

export async function logout(): Promise<void> {
  const refreshToken = useAuthStore.getState().refreshToken
  try {
    if (refreshToken) {
      await apiRequest('/v1/auth/logout', {
        method: 'POST',
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
    }
  } finally {
    useAuthStore.getState().clear()
  }
}

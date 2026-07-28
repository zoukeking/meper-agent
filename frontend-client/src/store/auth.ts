import { create } from 'zustand'

import type { AuthUser, TokenResponse } from '../types'

export const REFRESH_TOKEN_KEY = 'meper_client_refresh_token'
export const THEME_KEY = 'meper_client_theme'

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  user: AuthUser | null
  initialized: boolean
  theme: 'light' | 'dark'
  setSession: (bundle: TokenResponse) => void
  setInitialized: (value: boolean) => void
  clear: () => void
  toggleTheme: () => void
}

const storedTheme = (): 'light' | 'dark' => {
  const value = localStorage.getItem(THEME_KEY)
  if (value === 'light' || value === 'dark') return value
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: null,
  refreshToken: localStorage.getItem(REFRESH_TOKEN_KEY),
  user: null,
  initialized: false,
  theme: storedTheme(),
  setSession: (bundle) => {
    localStorage.setItem(REFRESH_TOKEN_KEY, bundle.refresh_token)
    set({
      accessToken: bundle.access_token,
      refreshToken: bundle.refresh_token,
      user: bundle.user,
    })
  },
  setInitialized: (value) => set({ initialized: value }),
  clear: () => {
    localStorage.removeItem(REFRESH_TOKEN_KEY)
    set({
      accessToken: null,
      refreshToken: null,
      user: null,
    })
  },
  toggleTheme: () => {
    const next = get().theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem(THEME_KEY, next)
    set({ theme: next })
  },
}))

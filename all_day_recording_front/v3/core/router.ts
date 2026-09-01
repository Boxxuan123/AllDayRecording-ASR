import { readonly, ref } from 'vue'

import { matchV3Route } from './routeMatch.js'

export type RouteName =
  | 'overview'
  | 'recordings'
  | 'session'
  | 'reviews'
  | 'reminders'
  | 'people'
  | 'processing'
  | 'devices'
  | 'data'
  | 'settings'
  | 'lab'

export interface AppRoute {
  name: RouteName
  path: string
  sessionId: string | null
  tab: string | null
}

export function matchRoute(location: Pick<Location, 'pathname' | 'search'>): AppRoute {
  return matchV3Route(location.pathname, location.search)
}

const current = ref(matchRoute(window.location))

window.addEventListener('popstate', () => {
  current.value = matchRoute(window.location)
})

export const route = readonly(current)

export function navigate(path: string): void {
  if (path === `${window.location.pathname}${window.location.search}`) return
  window.history.pushState({}, '', path)
  current.value = matchRoute(window.location)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const ROUTE_NAMES = {
  '/': 'overview',
  '/recordings': 'recordings',
  '/reviews': 'reviews',
  '/reminders': 'reminders',
  '/processing': 'processing',
  '/devices': 'devices',
  '/data': 'data',
  '/settings': 'settings',
  '/lab': 'lab',
}

export function matchV3Route(pathname, search = '') {
  const path = pathname.replace(/\/$/, '') || '/'
  const tab = new URLSearchParams(search).get('tab')
  const detail = path.match(/^\/recordings\/([^/]+)$/)
  if (detail) {
    return {
      name: 'session',
      path,
      sessionId: decodeURIComponent(detail[1]),
      tab,
    }
  }
  return {
    name: ROUTE_NAMES[path] ?? 'overview',
    path: ROUTE_NAMES[path] ? path : '/',
    sessionId: null,
    tab,
  }
}

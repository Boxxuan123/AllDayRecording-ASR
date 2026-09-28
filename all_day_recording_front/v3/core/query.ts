import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const cache = new Map<string, { value: unknown; at: number }>()
type Pending = { promise: Promise<unknown>; controller: AbortController; subscribers: Set<symbol> }
const inFlight = new Map<string, Pending>()
const versions = new Map<string, number>()
const TTL_MS = 30_000

export function useQuery<T>(key: string, loader: (signal: AbortSignal) => Promise<T>, enabled: () => boolean = () => true) {
  const subscriber = Symbol(key)
  const cached = cache.get(key)
  const data = ref<T | null>(cached ? cached.value as T : null)
  const error = ref<Error | null>(null)
  const loading = ref(!cached)
  const refreshing = ref(false)
  let generation = 0

  async function refresh(force = false): Promise<void> {
    const hit = cache.get(key)
    if (!force && hit && Date.now() - hit.at < TTL_MS) {
      data.value = hit.value as T
      loading.value = false
      return
    }
    const current = ++generation
    const version = versions.get(key) ?? 0
    loading.value = data.value === null
    refreshing.value = data.value !== null
    error.value = null
    try {
      let pending = inFlight.get(key)
      if (!pending) {
        const controller = new AbortController()
        pending = { promise: loader(controller.signal), controller, subscribers: new Set<symbol>() }
        inFlight.set(key, pending)
        void pending.promise.finally(() => {
          if (inFlight.get(key) === pending) inFlight.delete(key)
        }).catch(() => undefined)
      }
      pending.subscribers.add(subscriber)
      const value = await pending.promise as T
      if (current === generation && version === (versions.get(key) ?? 0)) {
        cache.set(key, { value, at: Date.now() })
        data.value = value
      }
    } catch (value) {
      if (current === generation && version === (versions.get(key) ?? 0)) {
        error.value = value instanceof Error ? value : new Error(String(value))
      }
    } finally {
      if (current === generation) {
        loading.value = false
        refreshing.value = false
      }
    }
  }

  onMounted(() => { if (enabled()) void refresh() })
  watch(enabled, (active) => {
    if (active) {
      void refresh()
    } else {
      generation += 1
      const pending = inFlight.get(key)
      pending?.subscribers.delete(subscriber)
      if (pending?.subscribers.size === 0) {
        inFlight.delete(key)
        pending.controller.abort()
      }
      loading.value = false
      refreshing.value = false
    }
  })
  onBeforeUnmount(() => {
    generation += 1
    const pending = inFlight.get(key)
    if (pending) {
      pending.subscribers.delete(subscriber)
      if (pending.subscribers.size === 0) {
        inFlight.delete(key)
        pending.controller.abort()
      }
    }
  })
  return {
    data,
    error,
    loading: computed(() => loading.value),
    refreshing: computed(() => refreshing.value),
    refresh,
  }
}

export function invalidateQuery(key: string): void {
  cache.delete(key)
  inFlight.get(key)?.controller.abort()
  inFlight.delete(key)
  versions.set(key, (versions.get(key) ?? 0) + 1)
}

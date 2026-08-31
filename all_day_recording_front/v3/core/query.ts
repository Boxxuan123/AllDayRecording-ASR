import { computed, onMounted, ref } from 'vue'

const cache = new Map<string, { value: unknown; at: number }>()

export function useQuery<T>(key: string, loader: () => Promise<T>) {
  const data = ref<T | null>(null)
  const error = ref<Error | null>(null)
  const loading = ref(true)

  async function refresh(force = false): Promise<void> {
    const hit = cache.get(key)
    if (!force && hit && Date.now() - hit.at < 30_000) {
      data.value = hit.value as T
      loading.value = false
      return
    }
    loading.value = true
    error.value = null
    try {
      const value = await loader()
      cache.set(key, { value, at: Date.now() })
      data.value = value
    } catch (value) {
      error.value = value instanceof Error ? value : new Error(String(value))
    } finally {
      loading.value = false
    }
  }

  onMounted(() => void refresh())
  return {
    data,
    error,
    loading: computed(() => loading.value),
    refresh,
  }
}

export function invalidateQuery(key: string): void {
  cache.delete(key)
}

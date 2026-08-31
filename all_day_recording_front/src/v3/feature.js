export function isV3Enabled(value) {
  const normalized = String(value ?? '').trim().toLowerCase()
  if (normalized === '') {
    return true
  }
  return ['1', 'true', 'yes', 'on'].includes(normalized)
}

export const V3_ENABLED = isV3Enabled(
  typeof import.meta.env === 'object'
    ? import.meta.env.VITE_ALLDAY_V3_ENABLED
    : undefined,
)

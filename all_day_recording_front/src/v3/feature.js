export function isV3Enabled(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value ?? '').toLowerCase())
}

export const V3_ENABLED = isV3Enabled(
  typeof import.meta.env === 'object'
    ? import.meta.env.VITE_ALLDAY_V3_ENABLED
    : undefined,
)

import type { Utterance } from './types'
export const soundKinds = [
  { value: 'speech', label: '正常发言（恢复）' },
  { value: 'live_speech', label: '确认是现场实际对话' },
  { value: 'overlapping_speech', label: '内容清楚但多人重叠（不作声纹）' },
  { value: 'remote_speech', label: '电话／会议中的实际互动者' },
  { value: 'media_speech', label: '确定是电视／视频播放（非实际互动）' },
  { value: 'non_speech', label: '非人声／噪声' },
  { value: 'unintelligible', label: '人声听不清' },
  { value: 'background_speech', label: '背景人声／电视声' },
]
export function soundKindLabel(item: Utterance): string {
  return soundKinds.find((kind) => kind.value === item.evidence.sound_kind)?.label ?? '正常发言'
}

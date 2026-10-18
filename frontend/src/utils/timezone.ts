/**
 * 时区展示工具。
 *
 * 后端统一存 IANA 时区名（如 Asia/Shanghai），前端展示时翻译成中文，
 * 并附上当前日期的 UTC 偏移（会自动体现夏令时）。
 */

export interface TimezoneOption {
  value: string
  label: string
}

export interface TimezoneGroup {
  label: string
  options: TimezoneOption[]
}

interface TimezoneDefinition {
  group: string
  value: string
  name: string
  short: string
}

const TIMEZONE_DEFINITIONS: TimezoneDefinition[] = [
  // 亚洲
  { group: '亚洲', value: 'Asia/Shanghai', name: '上海 · 北京时间', short: '上海' },
  { group: '亚洲', value: 'Asia/Hong_Kong', name: '香港 · 香港时间', short: '香港' },
  { group: '亚洲', value: 'Asia/Macau', name: '澳门 · 澳门时间', short: '澳门' },
  { group: '亚洲', value: 'Asia/Taipei', name: '台北 · 台北时间', short: '台北' },
  { group: '亚洲', value: 'Asia/Tokyo', name: '东京 · 日本时间', short: '东京' },
  { group: '亚洲', value: 'Asia/Seoul', name: '首尔 · 韩国时间', short: '首尔' },
  { group: '亚洲', value: 'Asia/Singapore', name: '新加坡 · 新加坡时间', short: '新加坡' },
  {
    group: '亚洲',
    value: 'Asia/Kuala_Lumpur',
    name: '吉隆坡 · 马来西亚时间',
    short: '吉隆坡',
  },
  { group: '亚洲', value: 'Asia/Bangkok', name: '曼谷 · 泰国时间', short: '曼谷' },
  { group: '亚洲', value: 'Asia/Jakarta', name: '雅加达 · 印尼西部时间', short: '雅加达' },
  { group: '亚洲', value: 'Asia/Manila', name: '马尼拉 · 菲律宾时间', short: '马尼拉' },
  {
    group: '亚洲',
    value: 'Asia/Ho_Chi_Minh',
    name: '胡志明市 · 越南时间',
    short: '胡志明市',
  },
  {
    group: '亚洲',
    value: 'Asia/Kolkata',
    name: '加尔各答 · 印度时间',
    short: '加尔各答',
  },
  { group: '亚洲', value: 'Asia/Dhaka', name: '达卡 · 孟加拉时间', short: '达卡' },
  { group: '亚洲', value: 'Asia/Karachi', name: '卡拉奇 · 巴基斯坦时间', short: '卡拉奇' },
  { group: '亚洲', value: 'Asia/Dubai', name: '迪拜 · 阿联酋时间', short: '迪拜' },
  { group: '亚洲', value: 'Asia/Riyadh', name: '利雅得 · 沙特时间', short: '利雅得' },
  { group: '亚洲', value: 'Asia/Tehran', name: '德黑兰 · 伊朗时间', short: '德黑兰' },
  {
    group: '亚洲',
    value: 'Asia/Jerusalem',
    name: '耶路撒冷 · 以色列时间',
    short: '耶路撒冷',
  },

  // 欧洲 / 非洲
  { group: '欧洲与非洲', value: 'Europe/London', name: '伦敦 · 英国时间', short: '伦敦' },
  { group: '欧洲与非洲', value: 'Europe/Paris', name: '巴黎 · 中欧时间', short: '巴黎' },
  { group: '欧洲与非洲', value: 'Europe/Berlin', name: '柏林 · 中欧时间', short: '柏林' },
  { group: '欧洲与非洲', value: 'Europe/Rome', name: '罗马 · 中欧时间', short: '罗马' },
  { group: '欧洲与非洲', value: 'Europe/Madrid', name: '马德里 · 中欧时间', short: '马德里' },
  {
    group: '欧洲与非洲',
    value: 'Europe/Amsterdam',
    name: '阿姆斯特丹 · 中欧时间',
    short: '阿姆斯特丹',
  },
  { group: '欧洲与非洲', value: 'Europe/Moscow', name: '莫斯科 · 俄罗斯时间', short: '莫斯科' },
  { group: '欧洲与非洲', value: 'Africa/Cairo', name: '开罗 · 埃及时间', short: '开罗' },
  {
    group: '欧洲与非洲',
    value: 'Africa/Johannesburg',
    name: '约翰内斯堡 · 南非时间',
    short: '约翰内斯堡',
  },

  // 美洲
  {
    group: '美洲',
    value: 'America/New_York',
    name: '纽约 · 美国东部时间',
    short: '纽约',
  },
  {
    group: '美洲',
    value: 'America/Toronto',
    name: '多伦多 · 加拿大东部时间',
    short: '多伦多',
  },
  {
    group: '美洲',
    value: 'America/Chicago',
    name: '芝加哥 · 美国中部时间',
    short: '芝加哥',
  },
  { group: '美洲', value: 'America/Denver', name: '丹佛 · 美国山地时间', short: '丹佛' },
  { group: '美洲', value: 'America/Phoenix', name: '凤凰城 · 美国山地时间', short: '凤凰城' },
  {
    group: '美洲',
    value: 'America/Los_Angeles',
    name: '洛杉矶 · 美国太平洋时间',
    short: '洛杉矶',
  },
  {
    group: '美洲',
    value: 'America/Vancouver',
    name: '温哥华 · 加拿大太平洋时间',
    short: '温哥华',
  },
  {
    group: '美洲',
    value: 'America/Mexico_City',
    name: '墨西哥城 · 墨西哥中部时间',
    short: '墨西哥城',
  },
  {
    group: '美洲',
    value: 'America/Sao_Paulo',
    name: '圣保罗 · 巴西利亚时间',
    short: '圣保罗',
  },

  // 大洋洲
  {
    group: '大洋洲',
    value: 'Australia/Sydney',
    name: '悉尼 · 澳大利亚东部时间',
    short: '悉尼',
  },
  {
    group: '大洋洲',
    value: 'Australia/Perth',
    name: '珀斯 · 澳大利亚西部时间',
    short: '珀斯',
  },
  {
    group: '大洋洲',
    value: 'Pacific/Auckland',
    name: '奥克兰 · 新西兰时间',
    short: '奥克兰',
  },
  { group: '大洋洲', value: 'Pacific/Honolulu', name: '檀香山 · 夏威夷时间', short: '檀香山' },

  // 其他
  { group: '其他', value: 'UTC', name: '协调世界时', short: 'UTC' },
]

const DEFINITION_BY_VALUE = new Map(TIMEZONE_DEFINITIONS.map((item) => [item.value, item]))

const GROUP_ORDER = ['亚洲', '欧洲与非洲', '美洲', '大洋洲', '其他']

function zoneOffset(timeZone: string): string {
  try {
    const value = new Intl.DateTimeFormat('en-US', {
      timeZone,
      timeZoneName: 'longOffset',
    })
      .formatToParts(new Date())
      .find((part) => part.type === 'timeZoneName')?.value

    if (!value) return ''
    if (value === 'GMT' || value === 'UTC') return 'UTC+00:00'
    return value.replace('GMT', 'UTC')
  } catch {
    return ''
  }
}

function withOffset(label: string, timeZone: string): string {
  const offset = zoneOffset(timeZone)
  return offset ? `${label}（${offset}）` : label
}

/** 中文完整时区名，适合下拉选项和设置页展示。 */
export function timezoneLabel(timeZone?: string | null): string {
  if (!timeZone) return '未设置'

  const definition = DEFINITION_BY_VALUE.get(timeZone)
  if (definition) return withOffset(definition.name, timeZone)

  // 兼容用户以前手填的、不在常用列表里的时区
  const offset = zoneOffset(timeZone)
  return offset ? `${timeZone}（${offset}）` : timeZone
}

/** 中文短时区名，适合侧边栏这类空间有限的位置。 */
export function timezoneShortLabel(timeZone?: string | null): string {
  if (!timeZone) return '未设置'

  const definition = DEFINITION_BY_VALUE.get(timeZone)
  return withOffset(definition?.short ?? timeZone, timeZone)
}

/**
 * 按地区分组的下拉选项。若当前值不在常用列表里，会补一个「其他」选项，
 * 避免旧数据打开设置页时被下拉框吞掉。
 */
export function timezoneGroups(current?: string | null): TimezoneGroup[] {
  const groups = GROUP_ORDER.map((label) => ({
    label,
    options: TIMEZONE_DEFINITIONS.filter((item) => item.group === label).map((item) => ({
      value: item.value,
      label: timezoneLabel(item.value),
    })),
  })).filter((group) => group.options.length > 0)

  if (current && !DEFINITION_BY_VALUE.has(current)) {
    const extra = groups.find((group) => group.label === '其他')
    const option = { value: current, label: timezoneLabel(current) }
    if (extra) extra.options.push(option)
    else groups.push({ label: '其他', options: [option] })
  }

  return groups
}

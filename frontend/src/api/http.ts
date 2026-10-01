/**
 * 极简 HTTP 客户端：统一注入 Bearer token、超时、错误信息提取。
 *
 * 默认走同源（开发时由 Vite 代理转发到 FastAPI）；
 * 如果前后端分开部署，用 VITE_API_BASE 指定后端地址即可。
 */

const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

const TOKEN_KEY = 'pa.token'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 隐私模式下 localStorage 不可用，忽略 */
  }
}

type UnauthorizedHandler = () => void

const unauthorizedHandlers: UnauthorizedHandler[] = []

/** 注册 401 处理（App 启动时挂一次，用来踢回登录页）。 */
export function onUnauthorized(handler: UnauthorizedHandler): void {
  unauthorizedHandlers.push(handler)
}

function notifyUnauthorized(): void {
  unauthorizedHandlers.forEach((handler) => handler())
}

type QueryValue = string | number | boolean | undefined | null

interface RequestOptions {
  json?: unknown
  params?: Record<string, QueryValue>
  timeoutMs?: number
}

type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

function parseBody(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/** 把 FastAPI 的 `detail` 提取成一句人话。 */
function extractDetail(data: unknown, fallback: string): string {
  if (typeof data === 'string' && data.trim()) return data.trim()
  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            const loc = (item as { loc?: unknown[] }).loc
            const where = Array.isArray(loc) ? loc.filter((p) => p !== 'body').join('.') : ''
            return `${where ? `${where}：` : ''}${String((item as { msg: unknown }).msg)}`
          }
          return String(item)
        })
        .filter(Boolean)
      if (parts.length) return parts.join('；')
    }
  }
  return fallback
}

async function request<T>(method: HttpMethod, path: string, options: RequestOptions = {}): Promise<T> {
  const url = new URL(`${API_BASE}${path}`, window.location.origin)
  if (options.params) {
    for (const [key, value] of Object.entries(options.params)) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value))
    }
  }

  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let body: string | undefined
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.json)
  }

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 30000)

  let response: Response
  try {
    response = await fetch(url.toString(), { method, headers, body, signal: controller.signal })
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') {
      throw new ApiError(0, '请求超时，请稍后重试或检查后端服务。')
    }
    throw new ApiError(0, '无法连接后端服务，请确认 uvicorn 已启动（默认 8000 端口）。')
  } finally {
    window.clearTimeout(timer)
  }

  const raw = await response.text()
  const data = raw ? parseBody(raw) : null

  if (response.status === 401) {
    setToken(null)
    notifyUnauthorized()
    throw new ApiError(401, extractDetail(data, '登录已过期，请重新登录。'))
  }

  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(data, `请求失败（HTTP ${response.status}）`))
  }

  return data as T
}

export const http = {
  get: <T>(path: string, options?: RequestOptions) => request<T>('GET', path, options),
  post: <T>(path: string, options?: RequestOptions) => request<T>('POST', path, options),
  put: <T>(path: string, options?: RequestOptions) => request<T>('PUT', path, options),
  patch: <T>(path: string, options?: RequestOptions) => request<T>('PATCH', path, options),
  delete: <T>(path: string, options?: RequestOptions) => request<T>('DELETE', path, options),
}

/** 判断错误是不是「资源不存在」，用于把 404 当成空状态处理。 */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}

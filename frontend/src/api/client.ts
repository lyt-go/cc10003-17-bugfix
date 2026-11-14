/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

/** 当前登录身份：发布链路按角色与业务归属鉴权，缺省按只读 analyst 处理。
 *  HTTP 头只允许 ASCII，中文业务归属/操作人按 UTF-8 百分号编码传输，后端解码。 */
const SESSION_HEADERS: Record<string, string> = {
  'X-Operator': encodeURIComponent('值班管理员'),
  'X-Role': 'release_manager',
  'X-Biz-Line': encodeURIComponent('增长业务线'),
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...SESSION_HEADERS, ...(init?.headers ?? {}) },
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

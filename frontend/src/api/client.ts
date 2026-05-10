// HTTP client for the RaceLink web layer. Mirrors the apiGet/apiPost/apiPut/
// apiDelete helpers in the legacy ``racelink/static/racelink.js`` so the
// migration is functionally a 1:1 swap.

const PLACEHOLDER = '{{ rl_base_path }}'

function readBasePathFromBody(): string {
  if (typeof document === 'undefined') return '/racelink'
  const raw = document.body?.dataset?.rlBasePath ?? '/racelink'
  // In ``vite dev`` the ``index.html`` placeholder is served as a literal
  // ``{{ rl_base_path }}`` because Flask isn't rendering it; substitute the
  // default so the proxy in vite.config.ts can match the route.
  if (!raw || raw === PLACEHOLDER) return '/racelink'
  return raw.replace(/\/$/, '')
}

const RL_BASE_PATH = readBasePathFromBody()

export function getBasePath(): string {
  return RL_BASE_PATH
}

export function withBasePath(path: string): string {
  if (!path) return RL_BASE_PATH || '/'
  if (/^https?:\/\//i.test(path)) return path
  let normalized = String(path).trim()
  if (normalized === '/racelink') {
    normalized = '/'
  } else if (normalized.startsWith('/racelink/')) {
    normalized = normalized.slice('/racelink'.length)
  }
  if (!normalized.startsWith('/')) {
    normalized = `/${normalized}`
  }
  return RL_BASE_PATH ? `${RL_BASE_PATH}${normalized}` : normalized
}

export interface ApiBag {
  ok?: boolean
  error?: string
  busy?: boolean
  __status?: number
  [key: string]: unknown
}

async function parseJsonSafe(res: Response): Promise<ApiBag> {
  try {
    return (await res.json()) as ApiBag
  } catch {
    return { ok: false, error: 'Bad JSON' }
  }
}

export async function apiGet(path: string): Promise<ApiBag> {
  const res = await fetch(withBasePath(path), { credentials: 'same-origin' })
  const j = await parseJsonSafe(res)
  j.__status = res.status
  return j
}

export async function apiPost(path: string, body?: unknown): Promise<ApiBag> {
  const res = await fetch(withBasePath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    credentials: 'same-origin',
  })
  const j = await parseJsonSafe(res)
  j.__status = res.status
  return j
}

export async function apiPut(path: string, body?: unknown): Promise<ApiBag> {
  const res = await fetch(withBasePath(path), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  })
  const j = await parseJsonSafe(res)
  j.__status = res.status
  return j
}

export async function apiDelete(path: string): Promise<ApiBag> {
  const res = await fetch(withBasePath(path), {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  })
  const j = await parseJsonSafe(res)
  j.__status = res.status
  return j
}

export async function apiUpload(path: string, formData: FormData): Promise<ApiBag> {
  const res = await fetch(withBasePath(path), {
    method: 'POST',
    body: formData,
    credentials: 'same-origin',
  })
  const j = await parseJsonSafe(res)
  j.__status = res.status
  return j
}

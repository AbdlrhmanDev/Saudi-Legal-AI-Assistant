// Thin client for the backend described in docs/api.md.

// Production is served by the API container on the same origin. Local Vite development keeps
// using the standalone backend unless VITE_API_URL is explicitly provided.
const BASE: string = import.meta.env.VITE_API_URL || (import.meta.env.PROD ? '' : 'http://localhost:8000')
const TOKEN_KEY = 'legal-ai-token'

export class ApiError extends Error {
  status: number
  code: string
  details: Record<string, unknown>
  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

let token: string | null = sessionStorage.getItem(TOKEN_KEY) ?? localStorage.getItem(TOKEN_KEY)
export const hasToken = () => token !== null
/** `remember` keeps the session across browser restarts; otherwise it ends with the tab. */
export function setToken(value: string | null, remember = false) {
  token = value
  sessionStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(TOKEN_KEY)
  if (value) (remember ? localStorage : sessionStorage).setItem(TOKEN_KEY, value)
}

type Options = { method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'; body?: unknown; form?: FormData; raw?: boolean }

export async function api<T>(path: string, { method = 'GET', body, form, raw }: Options = {}): Promise<T> {
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let resp: Response
  try {
    resp = await fetch(BASE + path, { method, headers, body: form ?? (body !== undefined ? JSON.stringify(body) : undefined) })
  } catch {
    throw new ApiError(0, 'NETWORK', 'تعذّر الاتصال بالخادم.')
  }
  const json = await resp.json().catch(() => null)
  if (raw) return json as T
  if (!resp.ok || !json?.success) {
    if (resp.status === 401 && token) {
      setToken(null)
      window.dispatchEvent(new Event('auth:expired'))
    }
    const err = json?.error ?? {}
    throw new ApiError(resp.status, err.code ?? 'HTTP_ERROR', err.message ?? 'حدث خطأ غير متوقع.', err.details ?? {})
  }
  return json.data as T
}

export type ChatStreamEvent =
  | { type: 'start' }
  | { type: 'delta'; delta: string }
  | { type: 'complete'; data: ChatAnswer }

/** Consume the POST NDJSON stream without buffering the full legal answer in the browser. */
export async function streamChat(
  body: { query: string; as_of_date?: string; domain?: string },
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'application/x-ndjson' }
  if (token) headers.Authorization = `Bearer ${token}`
  let resp: Response
  try {
    resp = await fetch(BASE + '/api/v1/chat/stream', { method: 'POST', headers, body: JSON.stringify(body) })
  } catch {
    throw new ApiError(0, 'NETWORK', 'تعذّر الاتصال بالخادم.')
  }
  if (!resp.ok) {
    const json = await resp.json().catch(() => null)
    const err = json?.error ?? {}
    throw new ApiError(resp.status, err.code ?? 'HTTP_ERROR', err.message ?? 'حدث خطأ غير متوقع.', err.details ?? {})
  }
  if (!resp.body) throw new ApiError(0, 'EMPTY_STREAM', 'لم يُرجع الخادم تدفقًا صالحًا.')

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const consumeLine = (line: string) => {
    if (!line.trim()) return
    const event = JSON.parse(line) as ChatStreamEvent | { type: 'error'; error?: { code?: string; message?: string } }
    if (event.type === 'error') {
      throw new ApiError(0, event.error?.code ?? 'STREAM_FAILED', event.error?.message ?? 'تعذّر إكمال الإجابة.')
    }
    onEvent(event)
  }

  for (;;) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) consumeLine(line)
    if (done) break
  }
  if (buffer.trim()) consumeLine(buffer)
}

export type Page<T> = { items: T[]; pagination: { page: number; limit: number; total: number; totalPages: number } }

export type User = { id: string; email: string; name: string | null; role: 'user' | 'admin' }

export type Citation = {
  law_id: string
  instrument_id: string
  article_id: string
  version_id: string
  source_url: string | null
  official_url: string | null
  law_title_ar: string
  article_number: string | null
  article_label: string | null
  chapter: string | null
  quote: string
  effective_from: string
  effective_to: string | null
  legal_status: string
  is_test_data: boolean
}

export type ChatAnswer = {
  answer_ar: string
  answer_status: 'grounded' | 'partial' | 'insufficient_evidence'
  as_of_date: string
  citations: Citation[]
  caveats: string[]
  trace_id: string
  disclaimer_ar: string
  forthcoming: { law_title_ar: string; version_id: string; effective_from: string }[]
  // Freshness contract (absent on answers saved before it existed).
  corpus_verified_at?: string | null
  coverage_notice?: string
  source_health?: SourceHealth
  // Official transaction links are helpful actions, not evidence for the legal answer.
  related_services?: RelatedService[]
}

export type RelatedService = {
  id: string
  title_ar: string
  description_ar: string
  guidance_ar: string
  provider_ar: string
  official_url: string
  official_info_url: string
  last_verified_at: string
  legal_evidence: false
}

export type SourceHealth = 'healthy' | 'delayed' | 'degraded'

export type Freshness = {
  corpus_verified_at: string | null
  last_run: { logical_date: string; status: string; trigger: string; finished_at: string | null } | null
  source_health: SourceHealth
  sources: { name: string; authority: string; status: SourceHealth; last_success_at: string | null; last_checked_at: string | null; is_test_data: boolean }[]
  covered_domains: string[]
  latest_included_publication: string | null
  active_generation: { number: number; activated_at: string | null } | null
  next_scheduled_sync: string
  timezone: string
  coverage_notice_ar: string
  warning_ar: string | null
}

export type SyncRun = {
  id: string
  logical_date_riyadh: string
  trigger: 'scheduled' | 'catch_up' | 'manual'
  status: string
  started_at: string | null
  finished_at: string | null
  retry_count: number
  retry_after: string | null
  stage_counts: Record<string, Record<string, number | null>>
  error_code: string | null
  errors: string[]
}

export type QuarantineItem = {
  id: string
  candidate_id: string
  candidate_title: string | null
  classification: string | null
  canonical_url: string | null
  reason_code: string
  detail: string | null
  created_at: string
  retry_after: string | null
  resolution_state: string
  is_test_data: boolean
}

export type Generation = {
  id: string
  number: number
  trigger: string
  validation_status: string
  is_active: boolean
  activated_at: string | null
  version_count: number
  article_count: number
  changed_chunk_count: number
  reused_chunk_count: number
  health_ok: boolean | null
  note: string | null
}

export type Law = {
  id: string
  official_title_ar: string
  domain: string
  issuing_authority: string | null
  instrument_type: string
  canonical_source_url: string | null
  is_test_data: boolean
}

export type Version = {
  id: string
  instrument_id: string
  version_label: string | null
  source_url: string | null
  source_authority: string | null
  issued_at?: string | null
  published_at: string | null
  effective_from: string | null
  effective_to: string | null
  legal_status: string
  article_count: number
  raw_hash: string
  is_test_data: boolean
  // admin only
  review_status?: string
  index_active?: boolean
  metadata_gaps?: string[]
  parse_warnings?: string[]
  instrument_title_ar?: string
  instrument_type?: string
  issuing_authority?: string | null
  domain?: string
  original_filename?: string | null
  mime_type?: string | null
}

export type Article = {
  id: string
  version_id: string
  article_number: string | null
  article_label: string | null
  chapter: string | null
  ordering: number
  original_text_ar: string
}

export type DocumentDetail = {
  version: Version
  law: Law
  articles: Article[]
  review_events: ReviewEvent[]
}

export type DiffChange = {
  article_number: string
  change: 'changed' | 'added' | 'removed'
  before: string | null
  after: string | null
}

export type Candidate = {
  id: string
  origin: string
  source_name: string | null
  canonical_url: string | null
  title: string
  summary?: string
  published_at: string | null
  discovered_at: string
  classification: string
  target_instrument_id: string | null
  target_instrument_title_ar: string | null
  proposed_version_id: string | null
  state: string
  error: string | null
  is_test_data: boolean
  affected_article_numbers: string[]
  diff?: { changes: DiffChange[]; warnings: string[] } | null
  evidence?: { gate?: { outcome: string; reasons: { code: string; detail: string }[] } } | null
}

export type ReviewEvent = { id: string; decision: string; decision_at: string; reason: string | null }

export type CandidateDetail = { candidate: Candidate; proposed_version: Version | null; review_events: ReviewEvent[] }

export type Job = {
  id: string
  job_type: string
  status: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  retry_count: number
  errors: string[]
  counts: Record<string, number>
  result: Record<string, unknown>
}

export type Source = {
  id: string
  name: string
  authority: string | null
  feed_url: string
  enabled: boolean
  crawl_policy: 'manual' | 'feed_only' | 'fetch_allowed' | 'fixture'
  check_interval: number
  last_checked_at: string | null
  last_success_at: string | null
  last_error: string | null
}

export type Trace = {
  id: string
  as_of_date: string
  response_status: string
  latency_ms: number
  cited_article_ids: string[]
  retrieved_count: number
}

export type Stats = {
  corpus: { instruments: number; versions_by_review_status: Record<string, number> }
  updates_by_state: Record<string, number>
  jobs_by_status: Record<string, number>
  sources: Source[]
  last_successful_source_check: string | null
  chat: { requests: number; latency_ms_p50: number | null; latency_ms_p95: number | null }
  worker: { alive: boolean; last_seen_at: string | null }
  sync: { last_run: SyncRun | null; open_quarantine: number; active_generation: Generation | null }
}

export type Coverage = { domains: { domain: string; instruments: number; articles: number }[]; notice_ar: string }

export type Evaluation = {
  generated_at: string
  corpus: string
  dataset: { total: number; by_class: Record<string, { total: number; passed: number }> }
  metrics: Record<string, number | null>
  targets: Record<string, number>
  targets_met: Record<string, boolean>
}

export type Ready = { status: string; checks: { database: string; pgvector: string; worker: string } }

/** Poll a queued job until the worker finishes it. */
export async function waitForJob(jobId: string, timeoutMs = 60_000): Promise<Job> {
  const started = Date.now()
  for (;;) {
    const job = await api<Job>(`/api/v1/admin/jobs/${jobId}`)
    if (job.status !== 'queued' && job.status !== 'running') return job
    if (Date.now() - started > timeoutMs) throw new ApiError(0, 'TIMEOUT', 'المهمة ما زالت قيد التنفيذ. تحقق من حالة العامل.')
    await new Promise((resolve) => setTimeout(resolve, 700))
  }
}

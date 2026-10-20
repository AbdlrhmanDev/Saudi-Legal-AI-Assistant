import { useEffect, useState } from 'react'
import { ApiError, api, waitForJob } from '../api'
import type { Evaluation, Generation, Job, Page, QuarantineItem, Ready, Source, Stats, SyncRun, Trace } from '../api'
import { Pill, TestBadge } from '../ui'
import { CLASS_AR, REASON_AR, fmtDate, fmtDateTime, shortId } from '../uiData'

const JOB_AR: Record<string, string> = { document_ingest: 'استيعاب وثيقة', feed_sync: 'فحص المصادر', reindex: 'إعادة فهرسة', nightly_sync: 'مزامنة كاملة' }
const TRIGGER_AR: Record<string, string> = { scheduled: 'مجدولة 00:00', catch_up: 'تعويض يوم فائت', manual: 'يدوية' }
const GEN_TRIGGER_AR: Record<string, string> = { nightly_sync: 'مزامنة ليلية', manual_approval: 'اعتماد يدوي', version_rollback: 'تراجع عن إصدار', rollback: 'استعادة إصدار فهرس', bootstrap: 'الإصدار الأولي' }
const runPill = (status: string) => (status === 'partial' ? 'run_partial' : status)

function runSummary(r: SyncRun): string {
  const s = r.stage_counts
  if (r.error_code) return `${r.error_code}${r.errors.length ? ` · ${r.errors[r.errors.length - 1]}` : ''}`
  const v = s.validate ?? {}
  const idx = s.index ?? {}
  return `${s.discover?.new ?? 0} جديد · نُشر ${v.publish ?? 0} · حُجز ${v.quarantine ?? 0} · استُبعد ${v.exclude ?? 0} · تضمينات جديدة ${idx.embedded ?? 0} (أعيد استخدام ${idx.reused ?? 0})`
}

function NightlySync({ onChanged }: { onChanged: () => void }) {
  const [runs, setRuns] = useState<SyncRun[]>([])
  const [quarantine, setQuarantine] = useState<QuarantineItem[]>([])
  const [generations, setGenerations] = useState<Generation[]>([])
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ text: string; warn: boolean } | null>(null)

  const load = () => {
    api<Page<SyncRun>>('/api/v1/admin/sync-runs?limit=7').then((p) => setRuns(p.items)).catch(() => undefined)
    api<Page<QuarantineItem>>('/api/v1/admin/quarantine?limit=8').then((p) => setQuarantine(p.items)).catch(() => undefined)
    api<Page<Generation>>('/api/v1/admin/index/generations?limit=4').then((p) => setGenerations(p.items)).catch(() => undefined)
  }
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(load, [])

  const act = async (work: () => Promise<{ text: string; warn: boolean }>) => {
    setBusy(true)
    setNote(null)
    try {
      setNote(await work())
    } catch (e) {
      setNote({ text: e instanceof Error ? e.message : 'تعذّر تنفيذ العملية.', warn: true })
    } finally {
      setBusy(false)
      load()
      onChanged()
    }
  }
  const runNow = () =>
    act(async () => {
      const queued = await api<{ job_id: string; run: SyncRun }>('/api/v1/admin/sync-runs/trigger', { method: 'POST' })
      const job = await waitForJob(queued.job_id, 600_000)
      const status = String(job.result.run_status ?? job.status)
      const stages = (job.result.stage_counts ?? {}) as SyncRun['stage_counts']
      const discovered = Number(stages.discover?.new ?? 0) + Number(stages.discover?.changed ?? 0)
      const published = Number(stages.validate?.publish ?? 0)
      const quarantined = Number(stages.validate?.quarantine ?? 0)
      const message = discovered === 0
        ? 'اكتملت المزامنة: لا توجد قوانين أو تحديثات جديدة.'
        : `اكتملت المزامنة: عُثر على ${discovered} جديد/متغيّر، ونُشر ${published} في RAG، وحُجز ${quarantined} للمراجعة.`
      return { text: message, warn: status === 'failed' || status === 'partial' }
    })
  const rollback = () => {
    const reason = window.prompt('سبب استعادة إصدار الفهرس السابق (يُسجَّل في سجل التدقيق):')
    if (!reason || reason.trim().length < 3) return
    act(async () => {
      const gen = await api<Generation>('/api/v1/admin/index/rollback', { method: 'POST', body: { reason: reason.trim() } })
      return { text: `أصبح إصدار الفهرس رقم ${gen.number} هو النشط. لم يُحذف أي نص.`, warn: false }
    })
  }

  const panel = { background: '#fff', border: '1px solid #E4E0D6', borderRadius: 10, overflow: 'hidden' } as const
  const head = { padding: '12px 16px', borderBottom: '1px solid #EEEAE1', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 } as const
  const row = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px 12px', padding: '9px 16px', borderBottom: '1px solid #F0ECE3', fontSize: 13 } as const
  const btn = { whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 8, padding: '5px 12px', fontSize: 12.5, fontWeight: 600, cursor: busy ? 'wait' : 'pointer', opacity: busy ? 0.55 : 1 } as const
  const active = generations.find((g) => g.is_active)

  return (
    <>
      <div style={panel}>
        <div style={head}>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, flex: 1 }}>المزامنة الليلية التلقائية</h2>
          <span style={{ fontSize: 12, color: '#5E655F' }}>كل يوم 00:00 بتوقيت الرياض · اكتشاف واعتماد وفهرسة RAG تلقائيًا</span>
          <button onClick={runNow} disabled={busy} style={{ ...btn, color: '#fff', background: '#1F5F4A', borderColor: '#1F5F4A' }}>
            {busy ? 'جارٍ…' : 'تشغيل المزامنة الآن'}
          </button>
        </div>
        {note && (
          <div dir="auto" style={{ padding: '8px 16px', fontSize: 12.5, borderBottom: '1px solid #F0ECE3', color: note.warn ? '#8A3A12' : '#1F5F4A', background: note.warn ? '#FBF1EA' : '#EEF6F1' }}>
            {note.text}
          </div>
        )}
        {runs.length === 0 && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F' }}>لم تُنفَّذ أي مزامنة بعد.</div>}
        {runs.map((r) => (
          <div key={r.id} style={row}>
            <span dir="ltr" className="mono" style={{ fontSize: 12, width: 90 }}>
              {r.logical_date_riyadh}
            </span>
            <Pill k={runPill(r.status)} size={11} dot={false} style={{ padding: '1px 8px' }} />
            <span style={{ fontSize: 12, color: '#5E655F', whiteSpace: 'nowrap' }}>
              {TRIGGER_AR[r.trigger] ?? r.trigger}
              {r.retry_count > 0 ? ` · ${r.retry_count} إعادة محاولة` : ''}
            </span>
            <span dir="auto" title={runSummary(r)} style={{ flex: '1 1 260px', minWidth: 0, fontSize: 12, color: r.error_code ? '#8A3A12' : '#3F4541', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {runSummary(r)}
            </span>
            <span style={{ fontSize: 11.5, color: '#5E655F', whiteSpace: 'nowrap' }}>{fmtDateTime(r.finished_at ?? r.started_at)}</span>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(420px,100%),1fr))', gap: 20 }}>
        <div style={panel}>
          <div style={head}>
            <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, flex: 1 }}>المحجوز آليًا</h2>
            <span style={{ fontSize: 12, color: '#5E655F' }}>لم يُنشر لأن أحد الفحوص الحتمية لم ينجح؛ راجعه في «التحديثات»</span>
          </div>
          {quarantine.length === 0 && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F' }}>لا توجد عناصر محجوزة.</div>}
          {quarantine.map((q) => (
            <div key={q.id} style={{ ...row, flexDirection: 'column', alignItems: 'stretch', gap: 2 }}>
              <span style={{ display: 'flex', gap: 8, alignItems: 'center', fontWeight: 600 }}>
                <span dir="auto" style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {q.candidate_title ?? shortId(q.candidate_id)}
                </span>
                {q.is_test_data && <TestBadge dashed />}
              </span>
              <span style={{ fontSize: 12, color: '#8A3A12' }}>{REASON_AR[q.reason_code] ?? q.reason_code}</span>
              <span style={{ fontSize: 11.5, color: '#5E655F' }}>
                {CLASS_AR[q.classification ?? ''] ?? q.classification} · {fmtDateTime(q.created_at)}
                {q.retry_after ? ` · إعادة المحاولة بعد ${fmtDateTime(q.retry_after)}` : ''}
              </span>
            </div>
          ))}
        </div>

        <div style={panel}>
          <div style={head}>
            <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, flex: 1 }}>إصدارات الفهرس</h2>
            {active && generations.length > 1 && (
              <button onClick={rollback} disabled={busy} style={{ ...btn, color: '#8A3A12' }}>
                استعادة الإصدار السابق
              </button>
            )}
          </div>
          {generations.map((g) => (
            <div key={g.id} style={row}>
              <span className="mono" dir="ltr" style={{ fontWeight: 700, width: 36 }}>
                #{g.number}
              </span>
              <Pill k={g.is_active ? 'active' : g.validation_status === 'failed' ? 'failed' : 'superseded'} size={11} dot={false} style={{ padding: '1px 8px' }} />
              <span style={{ flex: '1 1 200px', fontSize: 12.5 }}>
                {GEN_TRIGGER_AR[g.trigger] ?? g.trigger} · {g.version_count} إصدار · {g.changed_chunk_count} مقطع جديد / {g.reused_chunk_count} مُعاد استخدامه
              </span>
              <span style={{ fontSize: 11.5, color: '#5E655F', whiteSpace: 'nowrap' }}>{fmtDateTime(g.activated_at)}</span>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
const METRIC_AR: [string, string][] = [
  ['recall_at_5', 'استرجاع Recall@5'],
  ['citation_accuracy', 'دقة الاستشهاد'],
  ['auto_checked_correctness', 'صحة الإجابة (فحص آلي)'],
  ['abstention', 'الامتناع خارج المدونة'],
  ['temporal', 'صحة تاريخ النفاذ'],
]
const POLICY_AR: Record<string, string> = { manual: 'رفع يدوي', feed_only: 'متابعة الخلاصة', fetch_allowed: 'متابعة وجلب النص', fixture: 'بيانات تجريبية' }
const GREEN = '#2E7D5B'
const AMBER = '#C08A1E'
const RED = '#9B2C2C'

function jobNote(j: Job): { text: string; warn: boolean } {
  if (j.errors.length) return { text: j.errors[j.errors.length - 1], warn: true }
  const c = j.counts
  if (j.job_type === 'feed_sync') return { text: `${c.new ?? 0} جديدة · ${c.unchanged ?? 0} مكررة · ${c.acquired ?? 0} نص مجلوب`, warn: false }
  if (j.job_type === 'document_ingest') {
    const gaps = (j.result.metadata_gaps as string[] | undefined) ?? []
    if (j.status === 'duplicate') return { text: 'نفس البصمة؛ لم يُنشأ إصدار جديد', warn: true }
    return { text: `${c.articles ?? 0} مادة${gaps.includes('effective_from') ? ' · تاريخ النفاذ مفقود ← بحاجة لمراجعة' : ''}`, warn: gaps.includes('effective_from') }
  }
  return { text: c.chunks !== undefined ? `${c.chunks} مقطعًا محدّثًا` : '', warn: false }
}

const duration = (j: Job) => (j.started_at && j.finished_at ? ((new Date(j.finished_at).getTime() - new Date(j.started_at).getTime()) / 1000).toFixed(1) + 's' : '—')
const pct = (v: number | null | undefined) => (v == null ? '—' : `${Math.round(v * 100)}%`)

export function Ops() {
  const [ready, setReady] = useState<Ready | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [traces, setTraces] = useState<Trace[]>([])
  const [evaluation, setEvaluation] = useState<Evaluation | null | 'missing'>(null)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [sourceNote, setSourceNote] = useState<{ text: string; warn: boolean } | null>(null)

  const loadStats = () => api<Stats>('/api/v1/admin/stats').then(setStats).catch(() => setFailed(true))
  const loadJobs = () => api<Page<Job>>('/api/v1/admin/jobs?limit=6').then((p) => setJobs(p.items)).catch(() => undefined)

  const sourceAction = async (s: Source, work: () => Promise<{ text: string; warn: boolean }>) => {
    setBusy(s.id)
    setSourceNote(null)
    try {
      setSourceNote(await work())
    } catch (e) {
      setSourceNote({ text: e instanceof Error ? e.message : 'تعذّر تنفيذ العملية.', warn: true })
    } finally {
      setBusy(null)
      loadStats()
      loadJobs()
    }
  }
  const toggle = (s: Source) =>
    sourceAction(s, async () => {
      await api(`/api/v1/admin/sources/${s.id}`, { method: 'PATCH', body: { enabled: !s.enabled } })
      return { text: s.enabled ? 'أُوقفت المتابعة.' : 'فُعّلت المتابعة. سيُفحص المصدر دوريًا، أو اضغط «فحص الآن».', warn: false }
    })
  const checkNow = (s: Source) =>
    sourceAction(s, async () => {
      const queued = await api<{ job_id: string }>('/api/v1/admin/updates/sync', { method: 'POST', body: { source_id: s.id } })
      const job = await waitForJob(queued.job_id)
      if (job.errors.length) return { text: job.errors[job.errors.length - 1], warn: true }
      return { text: `اكتمل الفحص: ${job.counts.new ?? 0} عنصر جديد · ${job.counts.unchanged ?? 0} سبق رصده. راجعها في «التحديثات».`, warn: false }
    })

  useEffect(() => {
    api<Ready>('/health/ready', { raw: true }).then(setReady).catch(() => setFailed(true))
    loadStats()
    loadJobs()
    api<Page<Trace>>('/api/v1/admin/traces?limit=8').then((p) => setTraces(p.items)).catch(() => undefined)
    api<Evaluation>('/api/v1/admin/evaluation')
      .then(setEvaluation)
      .catch((e) => setEvaluation(e instanceof ApiError && e.status === 404 ? 'missing' : null))
  }, [])

  const failing = stats?.sources.filter((s) => s.enabled && s.last_error) ?? []
  const enabled = stats?.sources.filter((s) => s.enabled).length ?? 0
  const p95 = stats?.chat.latency_ms_p95
  const lastRun = stats?.sync?.last_run ?? null
  const health = [
    { name: 'واجهة API', note: p95 != null ? `p95 ${(p95 / 1000).toFixed(1)}s` : 'لا طلبات بعد', dot: failed ? RED : p95 != null && p95 > 8000 ? AMBER : GREEN },
    { name: 'قاعدة البيانات', note: ready?.checks.pgvector === 'ok' ? 'pgvector' : 'غير متاحة', dot: ready?.checks.database === 'ok' && ready.checks.pgvector === 'ok' ? GREEN : RED },
    { name: 'العامل', note: ready?.checks.worker === 'ok' ? 'يعمل' : 'متوقف', dot: ready?.checks.worker === 'ok' ? GREEN : RED },
    { name: 'مراقب المصادر', note: failing.length ? 'تعذّر الفحص' : enabled ? `${enabled} مصدر مفعّل` : 'لا مصادر مفعّلة', dot: failing.length ? AMBER : enabled ? GREEN : '#A8A69E' },
    {
      name: 'المزامنة الليلية',
      note: lastRun ? `${lastRun.logical_date_riyadh} · ${stats?.sync.open_quarantine ?? 0} محجوز` : 'لم تعمل بعد',
      dot: !lastRun ? '#A8A69E' : lastRun.status === 'failed' ? RED : lastRun.status === 'partial' ? AMBER : GREEN,
    },
  ]

  const panel = { background: '#fff', border: '1px solid #E4E0D6', borderRadius: 10, overflow: 'hidden' } as const
  const panelHead = { padding: '12px 16px', borderBottom: '1px solid #EEEAE1', display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: 8 } as const
  const TRACE_COLS = '110px 110px minmax(0,1fr) 140px 80px'

  return (
    <section className="pad-x" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '24px 32px 40px' }}>
      <div style={{ maxWidth: 1120, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20 }}>
        <header>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700 }}>العمليات والتقييم</h1>
          <p style={{ margin: '4px 0 0', color: '#5E655F', fontSize: 13 }}>حالة الخدمات، فحوص المصادر، المهام، ونتائج آخر تشغيل لمجموعة التقييم.</p>
        </header>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12 }}>
          {health.map((h) => (
            <div key={h.name} style={{ background: '#fff', border: '1px solid #E4E0D6', borderRadius: 10, padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ width: 9, height: 9, borderRadius: '50%', background: h.dot }} />
              <span style={{ flex: 1, fontWeight: 600, fontSize: 13.5 }}>{h.name}</span>
              <span style={{ whiteSpace: 'nowrap', fontSize: 12, color: '#5E655F' }}>{h.note}</span>
            </div>
          ))}
        </div>

        <NightlySync onChanged={loadStats} />

        <div style={panel}>
          <div style={panelHead}>
            <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>المصادر الرسمية</h2>
            <span style={{ fontSize: 12, color: '#5E655F' }}>المصادر المفعّلة تُزامَن كل ليلة. التغييرات التي تجتاز الفحوص الحتمية تُنشر تلقائيًا، وغير المؤكد منها يُحجز.</span>
          </div>
          {sourceNote && (
            <div dir="auto" style={{ padding: '8px 16px', fontSize: 12.5, borderBottom: '1px solid #F0ECE3', color: sourceNote.warn ? '#8A3A12' : '#1F5F4A', background: sourceNote.warn ? '#FBF1EA' : '#EEF6F1' }}>
              {sourceNote.text}
            </div>
          )}
          {stats?.sources.map((s) => {
            const manual = s.crawl_policy === 'manual'
            const btn = { whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 8, padding: '5px 12px', fontSize: 12.5, fontWeight: 600, cursor: 'pointer', opacity: busy ? 0.55 : 1 } as const
            return (
              <div key={s.id} style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, padding: '10px 16px', borderBottom: '1px solid #F0ECE3', fontSize: 13 }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', background: s.enabled ? (s.last_error ? AMBER : GREEN) : '#A8A69E' }} />
                <span style={{ flex: '1 1 260px', minWidth: 0, display: 'flex', flexDirection: 'column', lineHeight: 1.4 }}>
                  <span dir="auto" style={{ fontWeight: 600 }}>
                    {s.name}
                  </span>
                  <span style={{ fontSize: 11.5, color: '#5E655F' }}>
                    {POLICY_AR[s.crawl_policy] ?? s.crawl_policy}
                    {!manual && ` · كل ${Math.round(s.check_interval / 60)} ساعات · آخر فحص ناجح: ${fmtDateTime(s.last_success_at)}`}
                  </span>
                  {s.enabled && s.last_error && (
                    <span dir="auto" style={{ fontSize: 11.5, color: '#8A3A12', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={s.last_error}>
                      {s.last_error}
                    </span>
                  )}
                </span>
                {manual ? (
                  <span style={{ fontSize: 12, color: '#5E655F' }}>تُرفع وثائقه من «المدونة»</span>
                ) : (
                  <>
                    {s.enabled && (
                      <button onClick={() => checkNow(s)} disabled={busy !== null} style={{ ...btn, color: '#1F5F4A' }}>
                        {busy === s.id ? 'جارٍ…' : 'فحص الآن'}
                      </button>
                    )}
                    <button onClick={() => toggle(s)} disabled={busy !== null} style={{ ...btn, color: s.enabled ? '#8A3A12' : '#fff', background: s.enabled ? '#fff' : '#1F5F4A', borderColor: s.enabled ? '#D9D4C8' : '#1F5F4A' }}>
                      {s.enabled ? 'إيقاف' : 'تفعيل'}
                    </button>
                  </>
                )}
              </div>
            )
          })}
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(420px,100%),1fr))', gap: 20 }}>
          <div style={panel}>
            <div style={panelHead}>
              <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>المهام الأخيرة</h2>
            </div>
            {jobs.length === 0 && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F' }}>لا توجد مهام بعد.</div>}
            {jobs.map((j) => {
              const note = jobNote(j)
              return (
                <div key={j.id} style={{ display: 'grid', gridTemplateColumns: '90px minmax(0,1fr) 110px 70px', gap: 10, alignItems: 'center', padding: '9px 16px', borderBottom: '1px solid #F0ECE3', fontSize: 13 }}>
                  <span dir="ltr" className="mono" style={{ fontSize: 11.5, color: '#5E655F', textAlign: 'end' }}>
                    {shortId(j.id)}
                  </span>
                  <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.35, minWidth: 0 }}>
                    <span>
                      {JOB_AR[j.job_type] ?? j.job_type}
                      {j.retry_count > 0 ? ` · ${j.retry_count} إعادة محاولة` : ''}
                    </span>
                    <span dir="auto" style={{ fontSize: 11.5, color: note.warn ? '#8A3A12' : '#5E655F', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={note.text}>
                      {note.text}
                    </span>
                  </span>
                  <span>
                    <Pill k={j.status} size={11} dot={false} style={{ padding: '1px 8px' }} />
                  </span>
                  <span dir="ltr" className="mono" style={{ fontSize: 12, color: '#5E655F', textAlign: 'end' }}>
                    {duration(j)}
                  </span>
                </div>
              )
            })}
          </div>

          <div style={panel}>
            <div style={panelHead}>
              <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>التقييم</h2>
              {evaluation && evaluation !== 'missing' && (
                <span style={{ fontSize: 12, color: '#5E655F' }}>
                  {evaluation.dataset.total} حالة مفحوصة آليًا · تشغيل{' '}
                  <span dir="ltr" className="mono">
                    {evaluation.generated_at.slice(0, 10)}
                  </span>
                </span>
              )}
            </div>
            {evaluation === 'missing' && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F' }}>لا يوجد تقرير تقييم بعد. شغّل مجموعة التقييم لتوليده.</div>}
            {evaluation && evaluation !== 'missing' && (
              <>
                {METRIC_AR.map(([key, name]) => {
                  const ok = evaluation.targets_met[key]
                  return (
                    <div key={key} style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 70px 70px 24px', gap: 10, alignItems: 'center', padding: '9px 16px', borderBottom: '1px solid #F0ECE3', fontSize: 13 }}>
                      <span>{name}</span>
                      <span dir="ltr" className="mono" style={{ fontWeight: 600, textAlign: 'end' }}>
                        {pct(evaluation.metrics[key])}
                      </span>
                      <span dir="ltr" className="mono" style={{ color: '#5E655F', textAlign: 'end' }}>
                        ≥{pct(evaluation.targets[key])}
                      </span>
                      <span style={{ color: ok ? GREEN : RED, fontWeight: 700, textAlign: 'center' }}>{ok ? '✓' : '✕'}</span>
                    </div>
                  )
                })}
                {(() => {
                  const adv = evaluation.dataset.by_class.adversarial
                  return adv ? (
                    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 70px 70px 24px', gap: 10, alignItems: 'center', padding: '9px 16px', borderBottom: '1px solid #F0ECE3', fontSize: 13 }}>
                      <span>مقاومة حقن التعليمات</span>
                      <span dir="ltr" className="mono" style={{ fontWeight: 600, textAlign: 'end' }}>
                        {adv.passed}/{adv.total}
                      </span>
                      <span dir="ltr" className="mono" style={{ color: '#5E655F', textAlign: 'end' }}>
                        {adv.total}/{adv.total}
                      </span>
                      <span style={{ color: adv.passed === adv.total ? GREEN : RED, fontWeight: 700, textAlign: 'center' }}>{adv.passed === adv.total ? '✓' : '✕'}</span>
                    </div>
                  ) : null
                })()}
                <div style={{ padding: '8px 16px', fontSize: 11.5, color: '#5E655F' }}>الأعمدة: القيمة المقاسة · الهدف. القيم مقاسة على المدونة التجريبية وليست تقييمًا بشريًا على النصوص الرسمية.</div>
              </>
            )}
          </div>
        </div>

        <div style={panel}>
          <div style={{ padding: '12px 16px', borderBottom: '1px solid #EEEAE1' }}>
            <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>آثار الطلبات</h2>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <div style={{ minWidth: 620 }}>
              <div style={{ display: 'grid', gridTemplateColumns: TRACE_COLS, gap: 10, padding: '8px 16px', fontSize: 12, fontWeight: 600, color: '#5E655F', background: '#F7F5F0' }}>
                <span>المعرّف</span>
                <span>حتى تاريخ</span>
                <span>المواد المستشهد بها</span>
                <span>الحالة</span>
                <span>الزمن</span>
              </div>
              {traces.length === 0 && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F', borderTop: '1px solid #F0ECE3' }}>لا توجد طلبات مسجّلة بعد.</div>}
              {traces.map((t) => (
                <div key={t.id} style={{ display: 'grid', gridTemplateColumns: TRACE_COLS, gap: 10, alignItems: 'center', padding: '9px 16px', borderTop: '1px solid #F0ECE3', fontSize: 13 }}>
                  <span dir="ltr" className="mono" style={{ fontSize: 12, textAlign: 'end' }}>
                    {shortId(t.id)}
                  </span>
                  <span dir="ltr" className="mono" style={{ fontSize: 12, textAlign: 'end' }} title={fmtDate(t.as_of_date)}>
                    {t.as_of_date}
                  </span>
                  <span style={{ color: '#3F4541' }}>{t.cited_article_ids.length ? `${t.cited_article_ids.length} من ${t.retrieved_count} مواد مسترجعة` : '—'}</span>
                  <span>
                    <Pill k={t.response_status} size={11} dot={false} style={{ padding: '1px 8px' }} />
                  </span>
                  <span dir="ltr" className="mono" style={{ fontSize: 12, textAlign: 'end' }}>
                    {(t.latency_ms / 1000).toFixed(2)}s
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

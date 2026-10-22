import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, api, waitForJob } from '../api'
import type { Dashboard, Source, SourceLog } from '../api'
import { Icon, Pill } from '../ui'
import { ICON, fmtDateTime, fmtRiyadh } from '../uiData'
import type { Flash } from '../uiData'

const CATEGORY_AR: Record<string, string> = {
  A: 'المصادر النظامية الأساسية',
  B: 'التجارة والمالية والعمل والاستثمار والضرائب',
  C: 'العقار والبلديات والصحة والنقل',
  D: 'التنظيمات الرقمية والخصوصية والأمن السيبراني والملكية الفكرية',
  E: 'الأحكام القضائية والمبادئ',
  F: 'مصادر داعمة لرصد التحديثات',
}
const CONNECTOR_AR: Record<string, string> = {
  rss: 'خلاصة RSS رسمية',
  html_listing: 'قائمة وثائق',
  moj_judgments_api: 'واجهة الأحكام (وزارة العدل)',
  page_watch: 'رصد تغيّر الصفحة',
  none: 'لا يوجد وصول آلي مسموح',
}
const TIER_AR: Record<string, string> = { primary: 'أساسي', sectoral: 'قطاعي', judicial: 'قضائي', supporting: 'داعم' }
const FILTERS: [string, string][] = [
  ['all', 'الكل'],
  ['problem', 'تحتاج متابعة'],
  ['integrated', 'مدمجة'],
  ['discovery_only', 'رصد فقط'],
]
const PROBLEM = new Set(['unavailable', 'blocked', 'robots_disallowed', 'degraded'])

const panel = { background: '#fff', border: '1px solid #E4E0D6', borderRadius: 10, overflow: 'hidden' } as const
const head = { padding: '12px 16px', borderBottom: '1px solid #EEEAE1', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 } as const

function Tile({ label, value, hint, tone }: { label: string; value: string | number; hint?: string; tone?: 'warn' | 'ok' }) {
  const color = tone === 'warn' ? '#8E2626' : tone === 'ok' ? '#17543D' : '#1B1F1D'
  return (
    <div style={{ background: '#fff', border: '1px solid #E4E0D6', borderRadius: 10, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 12, color: '#5E655F' }}>{label}</span>
      <span style={{ fontSize: 22, fontWeight: 700, color, fontVariantNumeric: 'tabular-nums' }}>{value}</span>
      {hint && <span style={{ fontSize: 11.5, color: '#7A807B' }}>{hint}</span>}
    </div>
  )
}

const readableUrl = (url: string) => {
  try {
    return decodeURI(url)
  } catch {
    return url
  }
}
const pct = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)}%`)

export function Sources({ flash }: { flash: Flash }) {
  const [data, setData] = useState<Dashboard | null>(null)
  const [failed, setFailed] = useState(false)
  const [filter, setFilter] = useState('all')
  const [busy, setBusy] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)

  const load = useCallback(() => {
    api<Dashboard>('/api/v1/admin/dashboard')
      .then((d) => {
        setData(d)
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [])
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(load, [load])

  const run = async (key: string, work: () => Promise<string>) => {
    setBusy(key)
    try {
      flash(await work())
    } catch (e) {
      flash(e instanceof ApiError ? e.message : 'تعذّر تنفيذ العملية.', 'err')
    } finally {
      setBusy(null)
      load()
    }
  }
  const probe = (source?: Source) =>
    run(source ? `probe:${source.id}` : 'probe:all', async () => {
      const queued = await api<{ job_id: string }>('/api/v1/admin/sources/probe', { method: 'POST', body: source ? { source_id: source.id } : {} })
      const job = await waitForJob(queued.job_id, 900_000)
      const counts = Object.entries(job.counts).map(([k, v]) => `${v} ${k}`).join(' · ')
      return job.status === 'succeeded' ? `اكتمل فحص المصادر: ${counts}` : 'فشل فحص المصادر.'
    })
  const sync = (source: Source) =>
    run(`sync:${source.id}`, async () => {
      const queued = await api<{ job_id: string }>('/api/v1/admin/updates/sync', { method: 'POST', body: { source_id: source.id } })
      const job = await waitForJob(queued.job_id, 600_000)
      if (job.status !== 'succeeded') return `تعذّرت قراءة المصدر: ${job.errors.at(-1) ?? ''}`
      const c = job.counts
      return `قُرئ المصدر: ${c.new ?? 0} جديد · ${c.acquired ?? 0} جُلب نصه · ${c.deferred ?? 0} مؤجل · ${c.judicial_new ?? 0} حكم`
    })
  const toggle = (source: Source) =>
    run(`toggle:${source.id}`, async () => {
      await api(`/api/v1/admin/sources/${source.id}`, { method: 'PATCH', body: { enabled: !source.enabled } })
      return source.enabled ? 'أُوقف المصدر.' : 'فُعّل المصدر؛ سيُقرأ في المزامنة القادمة.'
    })

  const items = useMemo(() => {
    const all = data?.sources.items ?? []
    const shown = all.filter((s) =>
      filter === 'all' ? true : filter === 'problem' ? PROBLEM.has(s.health_status ?? '') : s.integration_status === filter,
    )
    return [...shown].sort((a, b) => (a.catalog_numbers?.[0] ?? 99) - (b.catalog_numbers?.[0] ?? 99))
  }, [data, filter])

  if (failed) return <div style={{ padding: 28, color: '#8E2626' }}>تعذّر تحميل لوحة المصادر.</div>
  if (!data) return <div style={{ padding: 28, color: '#5E655F' }}>جارٍ التحميل…</div>

  const s = data.sources
  const q = data.quality_last_30_days
  const btn = (key: string) =>
    ({ whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 8, padding: '4px 10px', fontSize: 12, fontWeight: 600, cursor: busy ? 'wait' : 'pointer', opacity: busy && busy !== key ? 0.55 : 1 }) as const
  const logsBySource = new Map<string, SourceLog>()
  for (const log of data.recent_source_logs) if (!logsBySource.has(log.source_id)) logsBySource.set(log.source_id, log)

  return (
    <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '24px clamp(16px,3vw,32px)', display: 'flex', flexDirection: 'column', gap: 20 }}>
      <header style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12 }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ margin: 0, fontSize: 20 }}>المصادر الرسمية</h1>
          <p style={{ margin: '4px 0 0', fontSize: 13, color: '#5E655F' }}>
            {s.catalog_entries} مصدرًا من القائمة المعتمدة · المزامنة يوميًا 00:00 بتوقيت الرياض · آخر مزامنة موثّقة:{' '}
            {data.last_successful_sync?.finished_at ? fmtRiyadh(data.last_successful_sync.finished_at) : 'لم تتم بعد'}
          </p>
        </div>
        <button onClick={() => probe()} disabled={!!busy} style={{ ...btn('probe:all'), color: '#fff', background: '#1F5F4A', borderColor: '#1F5F4A', padding: '7px 14px', fontSize: 13 }}>
          {busy === 'probe:all' ? 'جارٍ الفحص…' : 'فحص جميع المصادر الآن'}
        </button>
      </header>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))', gap: 10 }}>
        <Tile label="مصادر مسجلة" value={s.registered} hint={`${s.integrated} مدمج · ${s.discovery_only} رصد فقط · ${s.consolidated} مدمج مع غيره`} />
        <Tile label="متاحة" value={s.available} tone="ok" hint={`${s.degraded} بلا عناصر قابلة للاستخراج`} />
        <Tile label="غير متاحة أو محجوبة" value={s.unavailable + s.blocked} tone={s.unavailable + s.blocked ? 'warn' : undefined} hint={`${s.never_checked} لم تُفحص بعد`} />
        <Tile label="وثائق نظامية" value={data.corpus.instruments} hint={`${data.corpus.versions} إصدارًا`} />
        <Tile label="مواد مفهرسة" value={data.corpus.indexed_articles} hint={`${data.corpus.indexed_versions} إصدارًا نافذًا مفهرسًا`} />
        <Tile label="أحكام ومبادئ قضائية" value={data.corpus.judicial_records} hint="محفوظة منفصلة عن الأنظمة" />
        <Tile label="جديد خلال 7 أيام" value={data.updates_last_7_days.discovered} hint={`${data.updates_last_7_days.new_instruments} نظام جديد · ${data.updates_last_7_days.amended_instruments} معدّل · ${data.updates_last_7_days.repealed_instruments} ملغى`} />
        <Tile
          label="بانتظار التحقق"
          value={data.verification_queue.open_quarantine + data.verification_queue.needs_review}
          tone={data.verification_queue.open_quarantine ? 'warn' : undefined}
          hint={`${data.verification_queue.deferred_backlog} مؤجل للجلب · ${data.verification_queue.failed_fetches} تعذّر جلبه`}
        />
        <Tile label="مهام فاشلة" value={data.jobs.failed} tone={data.jobs.failed ? 'warn' : undefined} hint={`${data.jobs.queued} في الانتظار`} />
        <Tile label="جودة الإجابات (30 يومًا)" value={pct(q.cited_answer_rate)} hint={`إجابات موثقة بمصدر · ${q.questions} سؤالًا · إجابة ${pct(q.answer_rate)} · خارج النطاق ${pct(q.out_of_scope_rate)}`} />
      </div>

      <div style={panel}>
        <div style={head}>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, flex: 1 }}>سجل المصادر وحالة الموصلات</h2>
          <div role="tablist" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {FILTERS.map(([key, label]) => (
              <button key={key} role="tab" aria-selected={filter === key} onClick={() => setFilter(key)} style={{ border: '1px solid #D9D4C8', borderRadius: 20, padding: '3px 12px', fontSize: 12, background: filter === key ? '#1F5F4A' : '#fff', color: filter === key ? '#fff' : '#3F4541', cursor: 'pointer' }}>
                {label}
              </button>
            ))}
          </div>
        </div>
        {items.map((source, index) => {
          const showCategory = index === 0 || items[index - 1].category !== source.category
          const log = logsBySource.get(source.id)
          const expanded = open === source.id
          return (
            <div key={source.id}>
              {showCategory && filter === 'all' && (
                <div style={{ padding: '8px 16px', background: '#FAF9F6', borderBottom: '1px solid #EEEAE1', fontSize: 12, fontWeight: 700, color: '#5E655F' }}>
                  {source.category}. {CATEGORY_AR[source.category ?? ''] ?? ''}
                </div>
              )}
              <div style={{ padding: '10px 16px', borderBottom: '1px solid #F0ECE3', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px 12px', fontSize: 13 }}>
                <span className="mono" style={{ width: 34, color: '#7A807B', fontSize: 12 }}>{source.catalog_numbers?.join('،')}</span>
                <span style={{ flex: '1 1 260px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <span style={{ fontWeight: 600 }}>{source.name_ar ?? source.name}</span>
                  <span style={{ fontSize: 11.5, color: '#5E655F' }}>
                    {TIER_AR[source.authority_tier ?? ''] ?? ''} · {CONNECTOR_AR[source.connector_type ?? ''] ?? source.connector_type}
                    {source.last_http_status ? ` · HTTP ${source.last_http_status}` : ''}
                    {source.last_probe_at ? ` · فُحص ${fmtDateTime(source.last_probe_at)}` : ''}
                  </span>
                </span>
                <Pill k={source.health_status ?? 'unknown'} size={11} dot={false} />
                <Pill k={source.integration_status ?? 'integrated'} size={11} dot={false} />
                <span style={{ display: 'flex', gap: 6 }}>
                  <button onClick={() => setOpen(expanded ? null : source.id)} aria-expanded={expanded} style={btn('details')}>التفاصيل</button>
                  {!source.consolidated_into && <button onClick={() => probe(source)} disabled={!!busy} style={btn(`probe:${source.id}`)}>فحص</button>}
                  {source.enabled && <button onClick={() => sync(source)} disabled={!!busy} style={btn(`sync:${source.id}`)}>قراءة الآن</button>}
                  {source.connector_type !== 'none' && !source.consolidated_into && (
                    <button onClick={() => toggle(source)} disabled={!!busy} style={{ ...btn(`toggle:${source.id}`), color: source.enabled ? '#8E2626' : '#17543D' }}>
                      {source.enabled ? 'إيقاف' : 'تفعيل'}
                    </button>
                  )}
                </span>
              </div>
              {expanded && (
                <div dir="auto" style={{ padding: '10px 16px 14px 60px', borderBottom: '1px solid #F0ECE3', background: '#FCFBF8', display: 'grid', gap: 6, fontSize: 12.5, color: '#3F4541', lineHeight: 1.7 }}>
                  <span>
                    الرابط المعتمد:{' '}
                    <a href={source.official_url ?? source.feed_url} target="_blank" rel="noopener noreferrer" dir="ltr">{readableUrl(source.official_url ?? source.feed_url)}</a>
                    {source.official_url !== source.feed_url && (
                      <>
                        {' '}· نقطة القراءة: <a href={source.feed_url} target="_blank" rel="noopener noreferrer" dir="ltr">{readableUrl(source.feed_url)}</a>
                      </>
                    )}
                  </span>
                  {source.health_detail && <span><Icon d={ICON.info} size={13} /> آخر فحص: {source.health_detail}</span>}
                  {source.access_notes && <span>ملاحظات الوصول: {source.access_notes}</span>}
                  {source.last_error && <span style={{ color: '#8E2626' }}>آخر خطأ: {source.last_error}</span>}
                  {log && (
                    <span>
                      آخر تشغيل للموصل ({fmtDateTime(log.started_at)}): <Pill k={log.status} size={10.5} dot={false} />{' '}
                      {Object.entries(log.counts).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'لا عناصر'}
                      {log.error ? ` · ${log.error}` : ''}
                    </span>
                  )}
                  <span style={{ color: '#7A807B' }}>شروط إعادة الاستخدام: لم تُراجع قانونيًا لهذه النسخة ({source.licensing_status}).</span>
                </div>
              )}
            </div>
          )
        })}
        {items.length === 0 && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F' }}>لا توجد مصادر في هذا التصنيف.</div>}
      </div>

      <div style={panel}>
        <div style={head}>
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, flex: 1 }}>سجل التحديث اليومي لكل مصدر</h2>
          <span style={{ fontSize: 12, color: '#5E655F' }}>آخر {data.recent_source_logs.length} تشغيل</span>
        </div>
        {data.recent_source_logs.length === 0 && <div style={{ padding: '12px 16px', fontSize: 13, color: '#5E655F' }}>لم يُشغَّل أي موصل بعد.</div>}
        {data.recent_source_logs.map((log) => (
          <div key={log.id} style={{ padding: '8px 16px', borderBottom: '1px solid #F0ECE3', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '4px 12px', fontSize: 12.5 }}>
            <span style={{ width: 150, color: '#5E655F' }}>{fmtDateTime(log.started_at)}</span>
            <span style={{ flex: '1 1 200px', fontWeight: 600 }}>{log.source_name}</span>
            <Pill k={log.status} size={10.5} dot={false} />
            <span dir="auto" style={{ flex: '2 1 260px', color: log.error ? '#8E2626' : '#3F4541' }}>
              {log.error ?? (Object.entries(log.counts).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'لا تغيير')}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

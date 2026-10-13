import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { api, waitForJob } from '../api'
import type { Coverage, DocumentDetail, Page, Stats, Version } from '../api'
import { Empty, Icon, Pill } from '../ui'
import { DOMAIN_AR, DOT, GAP_AR, ICON, TYPE_AR, fmtDate, shortId } from '../uiData'
import type { Flash } from '../uiData'

type Row = { id: string; title: string; type: string; domain: string; authority: string; versions: Version[]; current: Version; status: string }

const COLS = 'minmax(0,2.4fr) minmax(0,1fr) minmax(0,1.7fr) 64px 110px 120px'

/** What a reviewer should see for one version: its review state unless it is authoritative. */
const statusOf = (v: Version) => (v.review_status === 'approved' && v.index_active ? v.legal_status : (v.review_status ?? v.legal_status))

function group(versions: Version[]): Row[] {
  const byLaw = new Map<string, Version[]>()
  for (const v of versions) byLaw.set(v.instrument_id, [...(byLaw.get(v.instrument_id) ?? []), v])
  return [...byLaw.entries()].map(([id, list]) => {
    const sorted = [...list].sort((a, b) => (a.effective_from ?? '9999').localeCompare(b.effective_from ?? '9999'))
    const current = sorted.find((v) => statusOf(v) === 'active') ?? sorted.find((v) => statusOf(v) === 'needs_review') ?? sorted[sorted.length - 1]
    return {
      id,
      title: current.instrument_title_ar ?? '—',
      type: TYPE_AR[current.instrument_type ?? ''] ?? 'وثيقة',
      domain: DOMAIN_AR[current.domain ?? ''] ?? '—',
      authority: current.issuing_authority ?? current.source_authority ?? '—',
      versions: sorted,
      current,
      status: statusOf(current),
    }
  })
}

export function Corpus({ flash, onChanged, onOpenReview }: { flash: Flash; onChanged: () => void; onOpenReview: () => void }) {
  const [versions, setVersions] = useState<Version[] | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [coverage, setCoverage] = useState<Coverage | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [viewing, setViewing] = useState<DocumentDetail | null>(null)
  const [editing, setEditing] = useState<Version | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Version | null>(null)
  const [actionBusy, setActionBusy] = useState(false)

  const load = useCallback(() => {
    api<Page<Version>>('/api/v1/admin/documents?limit=100')
      .then((p) => setVersions(p.items))
      .catch((e: Error) => flash(e.message, 'err'))
    api<Stats>('/api/v1/admin/stats').then(setStats).catch(() => undefined)
    api<Coverage>('/api/v1/coverage').then(setCoverage).catch(() => undefined)
  }, [flash])
  useEffect(load, [load])

  const rows = useMemo(() => group(versions ?? []), [versions])
  const row = rows.find((r) => r.id === selected) ?? rows[0]

  const cards = [
    { label: 'وثائق مفهرسة', value: stats?.corpus.instruments, icon: ICON.doc, tint: '#E6EFEA', fg: '#1F5F4A' },
    { label: 'مادة نظامية نافذة', value: coverage?.domains.reduce((n, d) => n + d.articles, 0), icon: ICON.list, tint: '#EEF1F6', fg: '#3B5378' },
    { label: 'بحاجة لمراجعة', value: stats ? (stats.corpus.versions_by_review_status.needs_review ?? 0) : undefined, icon: ICON.clock, tint: '#FBF0D9', fg: '#8A5A00' },
    { label: 'تحذيرات تكرار', value: stats ? (stats.jobs_by_status.duplicate ?? 0) : undefined, icon: ICON.warn, tint: '#F8E5E5', fg: '#8E2626' },
  ]
  const needsReview = stats?.corpus.versions_by_review_status.needs_review ?? 0

  const viewDocument = async (version: Version) => {
    setActionBusy(true)
    try {
      setViewing(await api<DocumentDetail>(`/api/v1/admin/documents/${version.id}`))
    } catch (err) {
      flash(err instanceof Error ? err.message : 'تعذّر عرض الوثيقة.', 'err')
    } finally {
      setActionBusy(false)
    }
  }

  const deleteDraft = async (version: Version) => {
    setActionBusy(true)
    try {
      await api(`/api/v1/admin/documents/${version.id}`, { method: 'DELETE' })
      flash('حُذفت المسودة غير المعتمدة.')
      setPendingDelete(null)
      setSelected(null)
      load()
      onChanged()
    } catch (err) {
      flash(err instanceof Error ? err.message : 'تعذّر حذف المسودة.', 'err')
    } finally {
      setActionBusy(false)
    }
  }

  return (
    <section className="pad-x" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '28px 32px 48px' }}>
      <div style={{ maxWidth: 1120, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 24 }}>
        <header style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 16 }}>
          <span style={{ width: 48, height: 48, borderRadius: 14, background: '#E6EFEA', color: '#1F5F4A', display: 'grid', placeItems: 'center', flex: 'none' }}>
            <Icon d={ICON.book} size={22} />
          </span>
          <div style={{ flex: 1, minWidth: 240, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700 }}>المدونة النظامية</h1>
            <p style={{ margin: 0, color: '#5E655F', fontSize: 13.5, textWrap: 'pretty' }}>الوثائق الرسمية المفهرسة وإصداراتها. لا يظهر في الإجابات إلا ما اعتُمد وكان نافذًا.</p>
          </div>
          {needsReview > 0 && (
            <button onClick={onOpenReview} style={{ whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 8, border: '1px solid #C9962D', background: '#FFF8E8', color: '#76510A', borderRadius: 10, padding: '10px 16px', fontSize: 14, fontWeight: 700, cursor: 'pointer' }}>
              <Icon d={ICON.check} size={17} sw={2} />
              مراجعة واعتماد ({needsReview})
            </button>
          )}
          <button onClick={() => setUploading(true)} style={{ whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 8, border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 600, cursor: 'pointer', boxShadow: '0 6px 16px -8px rgba(31,95,74,.6)' }}>
            <Icon d={ICON.upload} size={17} sw={2} stroke="#fff" />
            رفع وثيقة
          </button>
        </header>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(200px,1fr))', gap: 14 }}>
          {cards.map((c) => (
            <div key={c.label} className="card" style={{ padding: '16px 18px', display: 'flex', alignItems: 'center', gap: 14 }}>
              <span style={{ width: 44, height: 44, borderRadius: 12, background: c.tint, color: c.fg, display: 'grid', placeItems: 'center', flex: 'none' }}>
                <Icon d={c.icon} size={20} />
              </span>
              <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.25 }}>
                <span style={{ fontSize: 26, fontWeight: 700 }}>{c.value ?? '—'}</span>
                <span style={{ fontSize: 12.5, color: '#5E655F' }}>{c.label}</span>
              </div>
            </div>
          ))}
        </div>

        <div className="card" style={{ overflow: 'hidden' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '14px 20px', borderBottom: '1px solid #EEEAE1' }}>
            <h2 style={{ margin: 0, fontSize: 15.5, fontWeight: 700 }}>الوثائق</h2>
            <span style={{ fontSize: 12, color: '#5E655F', background: '#F3F1EC', borderRadius: 20, padding: '1px 9px' }}>{rows.length}</span>
            <span style={{ flex: 1 }} />
            <span style={{ fontSize: 12, color: '#5E655F' }}>اختر وثيقة لعرض إصداراتها</span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <div style={{ minWidth: 760 }}>
              <div role="row" style={{ display: 'grid', gridTemplateColumns: COLS, gap: 14, padding: '10px 20px', background: '#FAF9F6', fontSize: 12, fontWeight: 600, color: '#5E655F', borderBottom: '1px solid #EEEAE1' }}>
                <span>الوثيقة</span>
                <span>المجال</span>
                <span>الجهة</span>
                <span style={{ textAlign: 'end' }}>المواد</span>
                <span style={{ textAlign: 'end' }}>تاريخ النفاذ</span>
                <span>الحالة</span>
              </div>
              {versions !== null && rows.length === 0 && <div style={{ padding: 20, color: '#5E655F' }}>لا توجد وثائق بعد. ارفع أول وثيقة.</div>}
              {rows.map((r) => {
                const on = r.id === row?.id
                const dated = !!r.current.effective_from
                return (
                  <button key={r.id} className="law-row" onClick={() => setSelected(r.id)} aria-pressed={on} style={{ display: 'grid', width: '100%', textAlign: 'start', gridTemplateColumns: COLS, gap: 14, alignItems: 'center', padding: '14px 20px', border: 0, borderBottom: '1px solid #F0EDE6', boxShadow: on ? 'inset -3px 0 0 #1F5F4A' : 'none', background: on ? '#F3F8F5' : '#fff', fontSize: 13.5, color: '#1B1F1D', cursor: 'pointer' }}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
                      <span style={{ width: 36, height: 36, borderRadius: 10, background: on ? '#1F5F4A' : '#EEF3F0', color: on ? '#fff' : '#1F5F4A', display: 'grid', placeItems: 'center', flex: 'none' }}>
                        <Icon d={ICON.doc} size={17} />
                      </span>
                      <span style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.45, minWidth: 0 }}>
                        <span style={{ fontWeight: 600, fontSize: 14 }}>{r.title}</span>
                        <span style={{ fontSize: 12, color: '#5E655F' }}>
                          {r.type} · {r.versions.length} إصدارات
                        </span>
                      </span>
                    </span>
                    <span>
                      <span style={{ whiteSpace: 'nowrap', fontSize: 12, fontWeight: 500, color: '#3F4541', background: '#F3F1EC', borderRadius: 6, padding: '3px 9px' }}>{r.domain}</span>
                    </span>
                    <span style={{ color: '#3F4541', fontSize: 12.5, lineHeight: 1.5 }}>{r.authority}</span>
                    <span dir="ltr" className="mono" style={{ fontSize: 13, textAlign: 'end' }}>
                      {r.current.article_count}
                    </span>
                    <span style={{ whiteSpace: 'nowrap', fontSize: 12.5, textAlign: 'end', color: dated ? '#1B1F1D' : '#8A5A00' }}>{dated ? fmtDate(r.current.effective_from) : 'غير مؤكد'}</span>
                    <span>
                      <Pill k={r.status} style={{ padding: '3px 10px' }} />
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        </div>

        {row && <Timeline row={row} busy={actionBusy} onView={viewDocument} onEdit={setEditing} onDelete={setPendingDelete} />}
      </div>

      {uploading && (
        <UploadDialog
          flash={flash}
          onClose={() => setUploading(false)}
          onDone={() => {
            setUploading(false)
            load()
            onChanged()
          }}
        />
      )}
      {viewing && <DocumentDialog detail={viewing} onClose={() => setViewing(null)} />}
      {editing && (
        <EditDocumentDialog
          version={editing}
          flash={flash}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null)
            load()
            onChanged()
          }}
        />
      )}
      {pendingDelete && (
        <DeleteDraftDialog
          version={pendingDelete}
          busy={actionBusy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => deleteDraft(pendingDelete)}
        />
      )}
    </section>
  )
}

function Timeline({ row, busy, onView, onEdit, onDelete }: { row: Row; busy: boolean; onView: (version: Version) => void; onEdit: (version: Version) => void; onDelete: (version: Version) => void }) {
  return (
    <div className="card" style={{ padding: '20px 22px 22px' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px 14px', marginBottom: 22 }}>
        <span style={{ color: '#1F5F4A', display: 'flex' }}>
          <Icon d={ICON.clock} size={19} />
        </span>
        <h2 style={{ margin: 0, fontSize: 15.5, fontWeight: 700 }}>الخط الزمني للإصدارات</h2>
        <span style={{ fontSize: 13.5, fontWeight: 600, color: '#1F5F4A', background: '#E6EFEA', borderRadius: 6, padding: '2px 10px' }}>{row.title}</span>
        <span style={{ flex: 1 }} />
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#5E655F' }}>
          <Icon d={ICON.lock} size={14} />
          الإصدارات المعتمدة غير قابلة للتعديل
        </span>
      </div>
      <ol style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(210px,1fr))', rowGap: 20 }}>
        {row.versions.map((v, i) => {
          const st = statusOf(v)
          const isFuture = (s: string) => s === 'scheduled' || s === 'needs_review'
          const cur = st === 'active'
          const future = isFuture(st)
          const next = row.versions[i + 1]
          const from = v.effective_from ? fmtDate(v.effective_from) : ''
          const to = v.effective_to ? fmtDate(v.effective_to) : ''
          const range = from && to ? `${from} – ${to}` : cur && from ? `${from} – حتى الآن` : from ? `يبدأ ${from}` : 'تاريخ النفاذ غير محدد'
          const gaps = (v.metadata_gaps ?? []).map((g) => GAP_AR[g] ?? g)
          return (
            <li key={v.id} style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
              <span style={{ display: 'flex', alignItems: 'center', gap: 10, height: 24 }}>
                <span style={{ width: 24, height: 24, borderRadius: '50%', background: cur ? '#D5E6DC' : future ? '#FBF0D9' : '#EEEBE4', display: 'grid', placeItems: 'center', flex: 'none' }}>
                  <span style={{ width: 10, height: 10, borderRadius: '50%', background: DOT[st] ?? '#A8A69E' }} />
                </span>
                <span dir="ltr" className="mono" style={{ fontSize: 13, fontWeight: 600, color: cur ? '#1F5F4A' : future ? '#8A5A00' : '#5E655F' }}>
                  {v.effective_from ? v.effective_from.slice(0, 4) : '—'}
                </span>
                <span style={{ flex: 1, height: 0, borderTop: `2px ${next && isFuture(statusOf(next)) ? 'dashed' : 'solid'} ${cur ? '#A9CDB9' : '#DCD7CC'}`, opacity: next ? 1 : 0 }} />
              </span>
              <div style={{ marginInlineEnd: 14, flex: 1, border: `1px solid ${cur ? '#A9CDB9' : future ? '#EBD9AE' : '#E4E0D6'}`, background: cur ? '#F4F9F6' : '#fff', boxShadow: cur ? '0 8px 20px -12px rgba(31,95,74,.35)' : 'none', borderRadius: 12, padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span dir="ltr" className="mono" style={{ fontWeight: 600, fontSize: 12, color: '#5E655F', background: '#F3F1EC', borderRadius: 5, padding: '1px 7px' }}>
                    {shortId(v.id)}
                  </span>
                  <span style={{ flex: 1 }} />
                  <Pill k={st} size={11.5} style={{ gap: 5, padding: '2px 9px' }} />
                </span>
                <span style={{ fontSize: 14, fontWeight: 600, lineHeight: 1.55, textWrap: 'pretty' }}>{v.version_label ?? 'إصدار بلا تسمية'}</span>
                {gaps.length > 0 && <span style={{ fontSize: 12, color: '#8A3A12' }}>بيانات ناقصة: {gaps.join('، ')}</span>}
                <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#5E655F', paddingTop: 8, borderTop: `1px solid ${cur ? '#DCEBE2' : '#EEEAE1'}` }}>
                  <Icon d={ICON.calendar} size={13} />
                  {range}
                </span>
                <span style={{ display: 'flex', flexWrap: 'wrap', gap: 7, paddingTop: 4 }}>
                  <button type="button" disabled={busy} onClick={() => onView(v)} style={smallAction}>عرض الوثيقة</button>
                  {v.source_url && <a href={v.source_url} target="_blank" rel="noopener noreferrer" style={{ ...smallAction, textDecoration: 'none' }}>المصدر الرسمي</a>}
                  {v.review_status === 'needs_review' && <button type="button" disabled={busy} onClick={() => onEdit(v)} style={smallAction}>تعديل البيانات</button>}
                  {v.review_status === 'needs_review' && <button type="button" disabled={busy} onClick={() => onDelete(v)} style={{ ...smallAction, color: '#8E2626', borderColor: '#E7C9C9' }}>حذف المسودة</button>}
                </span>
              </div>
            </li>
          )
        })}
      </ol>
      {row.versions.length === 0 && <Empty>لا توجد إصدارات.</Empty>}
    </div>
  )
}

const smallAction = { border: '1px solid #D9D4C8', background: '#fff', color: '#1F5F4A', borderRadius: 7, padding: '5px 9px', fontSize: 11.5, fontWeight: 600, cursor: 'pointer' } as const

function DeleteDraftDialog({ version, busy, onCancel, onConfirm }: { version: Version; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  return (
    <div role="alertdialog" aria-modal="true" aria-labelledby="delete-draft-title" aria-describedby="delete-draft-description" onKeyDown={(e) => e.key === 'Escape' && !busy && onCancel()} style={{ position: 'fixed', inset: 0, background: 'rgba(27,31,29,.42)', display: 'grid', placeItems: 'center', zIndex: 40, padding: 16 }}>
      <div className="card" style={{ width: 'min(470px,100%)', borderRadius: 16, padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div style={{ width: 44, height: 44, borderRadius: 12, color: '#8E2626', background: '#F8E5E5', display: 'grid', placeItems: 'center' }}><Icon d={ICON.warn} size={21} /></div>
        <div>
          <h2 id="delete-draft-title" style={{ margin: '0 0 7px', fontSize: 19 }}>حذف المسودة؟</h2>
          <p id="delete-draft-description" style={{ margin: 0, color: '#5E655F', lineHeight: 1.8, fontSize: 13.5 }}>سيتم حذف مسودة «{version.instrument_title_ar ?? 'الوثيقة'}» وموادها. لا يمكن التراجع عن هذا الإجراء، ولا يشمل الحذف أي إصدار معتمد.</p>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button type="button" disabled={busy} onClick={onConfirm} style={{ border: 0, background: '#A43737', color: '#fff', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 700, cursor: busy ? 'wait' : 'pointer', opacity: busy ? .6 : 1 }}>{busy ? 'جارٍ الحذف…' : 'نعم، احذف المسودة'}</button>
          <button type="button" disabled={busy} onClick={onCancel} autoFocus style={{ border: '1px solid #D9D4C8', background: '#fff', color: '#1B1F1D', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 600, cursor: 'pointer' }}>إلغاء</button>
        </div>
      </div>
    </div>
  )
}

function DocumentDialog({ detail, onClose }: { detail: DocumentDetail; onClose: () => void }) {
  return (
    <div role="dialog" aria-modal="true" aria-label="عرض الوثيقة" onKeyDown={(e) => e.key === 'Escape' && onClose()} style={{ position: 'fixed', inset: 0, background: 'rgba(27,31,29,.35)', display: 'grid', placeItems: 'center', zIndex: 30, padding: 16 }}>
      <div className="card" style={{ width: 'min(900px,100%)', maxHeight: '92vh', overflow: 'hidden', display: 'flex', flexDirection: 'column', borderRadius: 16 }}>
        <header style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '18px 22px', borderBottom: '1px solid #EEEAE1' }}>
          <div style={{ flex: 1 }}>
            <h2 style={{ margin: 0, fontSize: 19 }}>{detail.law.official_title_ar}</h2>
            <p style={{ margin: '5px 0 0', color: '#5E655F', fontSize: 12.5 }}>{detail.version.version_label ?? 'إصدار بلا تسمية'} · {detail.articles.length} مادة</p>
          </div>
          {detail.version.source_url && <a href={detail.version.source_url} target="_blank" rel="noopener noreferrer" style={{ ...smallAction, textDecoration: 'none' }}>فتح المصدر الرسمي</a>}
          <button type="button" onClick={onClose} aria-label="إغلاق" style={{ border: 0, background: '#F3F1EC', borderRadius: 8, width: 32, height: 32, cursor: 'pointer', display: 'grid', placeItems: 'center' }}><Icon d={ICON.close} size={16} /></button>
        </header>
        <div style={{ overflowY: 'auto', padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          {(detail.version.parse_warnings?.length ?? 0) > 0 && <div role="alert" style={{ background: '#FBF5E6', border: '1px solid #F0DFB5', color: '#5A4210', padding: '10px 13px', borderRadius: 9, fontSize: 12.5 }}>تحذيرات الاستخراج: {detail.version.parse_warnings?.join('، ')}</div>}
          {detail.articles.map((article) => (
            <article key={article.id} style={{ border: '1px solid #E4E0D6', borderRadius: 12, padding: '14px 16px' }}>
              <h3 style={{ margin: '0 0 8px', color: '#1F5F4A', fontSize: 15 }}>{article.article_label ?? (article.article_number ? `المادة ${article.article_number}` : `المقطع ${article.ordering}`)}</h3>
              {article.chapter && <div style={{ color: '#5E655F', fontSize: 12, marginBottom: 7 }}>{article.chapter}</div>}
              <p className="naskh" style={{ margin: 0, whiteSpace: 'pre-wrap', lineHeight: 2, fontSize: 16 }}>{article.original_text_ar}</p>
            </article>
          ))}
        </div>
      </div>
    </div>
  )
}

function EditDocumentDialog({ version, flash, onClose, onDone }: { version: Version; flash: Flash; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const label = { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 } as const
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const form = new FormData(e.currentTarget)
    const value = (name: string) => String(form.get(name) ?? '').trim()
    setBusy(true)
    try {
      await api(`/api/v1/admin/documents/${version.id}`, {
        method: 'PATCH',
        body: {
          title_ar: value('title_ar'), domain: value('domain'), instrument_type: value('instrument_type'),
          issuing_authority: value('issuing_authority') || null, source_url: value('source_url') || null,
          version_label: value('version_label') || null, published_at: value('published_at') || null,
          effective_from: value('effective_from') || null,
        },
      })
      flash('حُفظت بيانات المسودة.')
      onDone()
    } catch (err) {
      flash(err instanceof Error ? err.message : 'تعذّر حفظ البيانات.', 'err')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div role="dialog" aria-modal="true" aria-label="تعديل بيانات الوثيقة" onKeyDown={(e) => e.key === 'Escape' && onClose()} style={{ position: 'fixed', inset: 0, background: 'rgba(27,31,29,.35)', display: 'grid', placeItems: 'center', zIndex: 30, padding: 16 }}>
      <form onSubmit={submit} className="card" style={{ width: 'min(620px,100%)', maxHeight: '92vh', overflowY: 'auto', padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 14, borderRadius: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}><h2 style={{ margin: 0, flex: 1, fontSize: 18 }}>تعديل بيانات المسودة</h2><button type="button" onClick={onClose} aria-label="إغلاق" style={{ border: 0, background: '#F3F1EC', borderRadius: 8, width: 32, height: 32, cursor: 'pointer', display: 'grid', placeItems: 'center' }}><Icon d={ICON.close} size={16} /></button></div>
        <div style={{ background: '#FBF5E6', border: '1px solid #F0DFB5', padding: '10px 13px', borderRadius: 9, fontSize: 12.5, color: '#5A4210' }}>يمكن تعديل البيانات الوصفية فقط. لتغيير النص القانوني ارفع إصدارًا جديدًا.</div>
        <label style={label}>العنوان<input className="field" name="title_ar" required defaultValue={version.instrument_title_ar ?? ''} /></label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12 }}>
          <label style={label}>المجال<select className="field" name="domain" defaultValue={version.domain ?? 'other'}>{Object.entries(DOMAIN_AR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label style={label}>نوع الوثيقة<select className="field" name="instrument_type" defaultValue={version.instrument_type ?? 'law'}>{Object.entries(TYPE_AR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label style={label}>الجهة المصدرة<input className="field" name="issuing_authority" defaultValue={version.issuing_authority ?? version.source_authority ?? ''} /></label>
          <label style={label}>تسمية الإصدار<input className="field" name="version_label" defaultValue={version.version_label ?? ''} /></label>
          <label style={label}>تاريخ النشر<input className="field" type="date" name="published_at" defaultValue={version.published_at?.slice(0, 10) ?? ''} /></label>
          <label style={label}>تاريخ النفاذ<input className="field" type="date" name="effective_from" defaultValue={version.effective_from?.slice(0, 10) ?? ''} /></label>
        </div>
        <label style={label}>رابط المصدر الرسمي<input className="field" dir="ltr" type="url" name="source_url" defaultValue={version.source_url ?? ''} /></label>
        <div style={{ display: 'flex', gap: 10 }}><button type="submit" disabled={busy} style={{ border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 10, padding: '10px 20px', fontSize: 14, fontWeight: 600, cursor: 'pointer', opacity: busy ? .55 : 1 }}>{busy ? 'جارٍ الحفظ…' : 'حفظ التعديلات'}</button><button type="button" onClick={onClose} style={{ border: '1px solid #D9D4C8', background: '#fff', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 600, cursor: 'pointer' }}>إلغاء</button></div>
      </form>
    </div>
  )
}

function UploadDialog({ flash, onClose, onDone }: { flash: Flash; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const label = { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 } as const

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const raw = new FormData(e.currentTarget)
    const form = new FormData()
    for (const [key, value] of raw.entries()) {
      if (value instanceof File ? value.size > 0 : String(value).trim() !== '') form.append(key, value)
    }
    if (!form.has('file') && !form.has('register_url')) return flash('اختر ملفًا أو أدخل رابط صفحة رسمية.', 'err')
    if (form.has('file') && form.has('register_url')) return flash('استخدم الملف أو الرابط، لا كليهما.', 'err')
    setBusy(true)
    try {
      const queued = await api<{ job_id: string }>('/api/v1/admin/documents', { method: 'POST', form })
      const job = await waitForJob(queued.job_id)
      if (job.status === 'succeeded') {
        const gaps = ((job.result.metadata_gaps as string[]) ?? []).map((g) => GAP_AR[g] ?? g)
        flash(`استُوعبت الوثيقة (${job.counts.articles ?? 0} مادة) وأُضيفت إلى قائمة المراجعة.${gaps.length ? ` بيانات ناقصة: ${gaps.join('، ')}.` : ''}`)
        onDone()
      } else if (job.status === 'duplicate') {
        flash('الملف مطابق لإصدار موجود؛ لم يُنشأ إصدار جديد.')
        onDone()
      } else {
        flash(`فشل الاستيعاب: ${job.errors[job.errors.length - 1] ?? 'خطأ غير معروف'}`, 'err')
      }
    } catch (err) {
      flash(err instanceof Error ? err.message : 'تعذّر الرفع.', 'err')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="رفع وثيقة" onKeyDown={(e) => e.key === 'Escape' && onClose()} style={{ position: 'fixed', inset: 0, background: 'rgba(27,31,29,.35)', display: 'grid', placeItems: 'center', zIndex: 20, padding: 16 }}>
      <form onSubmit={submit} className="card" style={{ width: 'min(620px, 100%)', maxHeight: '92vh', overflowY: 'auto', padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 14, borderRadius: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', lineHeight: 1.4 }}>
            <span style={{ fontSize: 17, fontWeight: 700 }}>رفع وثيقة</span>
            <span style={{ fontSize: 12, color: '#5E655F' }}>PDF أو HTML أو نص حتى 20MB، أو رابط صفحة من جهة معتمدة. تدخل الوثيقة قائمة المراجعة ولا تظهر في الإجابات قبل اعتمادها.</span>
          </div>
          <button type="button" onClick={onClose} aria-label="إغلاق" style={{ border: 0, background: '#F3F1EC', borderRadius: 8, width: 32, height: 32, cursor: 'pointer', color: '#3F4541', display: 'grid', placeItems: 'center', flex: 'none' }}>
            <Icon d={ICON.close} size={16} />
          </button>
        </div>
        <label style={label}>
          الملف
          <input className="field" type="file" name="file" accept=".pdf,.txt,.html,.htm,application/pdf,text/plain,text/html" />
        </label>
        <label style={label}>
          أو رابط صفحة رسمية
          <input className="field" dir="ltr" type="url" name="register_url" placeholder="https://laws.boe.gov.sa/…" />
        </label>
        <label style={label}>
          عنوان الوثيقة <span style={{ fontWeight: 400, color: '#5E655F' }}>يُقرأ من النص إن تُرك فارغًا</span>
          <input className="field" name="title_ar" maxLength={500} />
        </label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12 }}>
          <label style={label}>
            المجال
            <select className="field" name="domain" defaultValue="">
              <option value="">—</option>
              {Object.entries(DOMAIN_AR).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label style={label}>
            نوع الوثيقة
            <select className="field" name="instrument_type" defaultValue="">
              <option value="">—</option>
              {Object.entries(TYPE_AR).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label style={label}>
            الجهة المصدرة
            <input className="field" name="issuing_authority" maxLength={300} />
          </label>
          <label style={label}>
            تسمية الإصدار
            <input className="field" name="version_label" maxLength={200} />
          </label>
          <label style={label}>
            تاريخ النشر
            <input className="field" type="date" name="published_at" />
          </label>
          <label style={label}>
            تاريخ النفاذ <span style={{ fontWeight: 400, color: '#5E655F' }}>اتركه فارغًا إن لم يكن مؤكدًا</span>
            <input className="field" type="date" name="effective_from" />
          </label>
        </div>
        <label style={label}>
          رابط المصدر الرسمي
          <input className="field" dir="ltr" type="url" name="source_url" placeholder="https://…" />
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <input type="checkbox" name="is_test_data" value="true" style={{ width: 16, height: 16, accentColor: '#1F5F4A' }} />
          بيانات تجريبية (ليست وثيقة رسمية)
        </label>
        <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-start' }}>
          <button type="submit" disabled={busy} style={{ border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 10, padding: '10px 20px', fontSize: 14, fontWeight: 600, cursor: 'pointer', opacity: busy ? 0.55 : 1 }}>
            {busy ? 'جارٍ الاستيعاب…' : 'رفع وإرسال للمراجعة'}
          </button>
          <button type="button" onClick={onClose} style={{ border: '1px solid #D9D4C8', background: '#fff', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 600, cursor: 'pointer', color: '#1B1F1D' }}>
            إلغاء
          </button>
        </div>
      </form>
    </div>
  )
}

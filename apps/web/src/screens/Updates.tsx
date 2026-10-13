import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, api, waitForJob } from '../api'
import type { Candidate, CandidateDetail, DiffChange, Page, Source } from '../api'
import { Empty, Icon, Pill, TestBadge } from '../ui'
import { CLASS_AR, ICON, WARNING_AR, fmtDate, fmtDateTime, pillOf, shortId } from '../uiData'
import type { Flash } from '../uiData'

const BLOCKING = Object.keys(WARNING_AR)
const OPEN_STATES = ['candidate', 'retrieved', 'parsed', 'needs_review', 'failed']
const pillKey = (state: string) => (state === 'needs_review' ? 'pending' : state)

/** Word-level common prefix/suffix, so only the changed span is marked. */
function inlineDiff(before: string, after: string) {
  const a = before.split(/(\s+)/)
  const b = after.split(/(\s+)/)
  let p = 0
  while (p < a.length && p < b.length && a[p] === b[p]) p++
  let s = 0
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++
  const clip = (text: string, fromEnd: boolean) => (text.length <= 160 ? text : fromEnd ? '…' + text.slice(-160) : text.slice(0, 160) + '…')
  return {
    pre: clip(a.slice(0, p).join(''), true),
    del: a.slice(p, a.length - s).join(''),
    ins: b.slice(p, b.length - s).join(''),
    post: clip(a.slice(a.length - s).join(''), false),
  }
}

export function Updates({ flash, onChanged, asOf }: { flash: Flash; onChanged: () => void; asOf: string }) {
  const [items, setItems] = useState<Candidate[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<CandidateDetail | null>(null)
  const [sources, setSources] = useState<Source[]>([])
  const [busy, setBusy] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [effective, setEffective] = useState('')
  const [published, setPublished] = useState('')
  const [evidence, setEvidence] = useState('')
  const [reason, setReason] = useState('')
  const [attaching, setAttaching] = useState(false)

  const loadList = useCallback(async () => {
    try {
      const page = await api<Page<Candidate>>('/api/v1/admin/updates?limit=50')
      setItems(page.items)
      setSelected((cur) => (cur && page.items.some((i) => i.id === cur) ? cur : (page.items.find((i) => i.state === 'needs_review')?.id ?? page.items[0]?.id ?? null)))
      setSources((await api<{ items: Source[] }>('/api/v1/admin/sources')).items)
    } catch (e) {
      flash(e instanceof Error ? e.message : 'تعذّر التحميل.', 'err')
    }
  }, [flash])
  // Fetching the server-backed queue is the synchronization this effect owns.
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(() => void loadList(), [loadList])

  const loadDetail = useCallback(async (id: string) => {
    const d = await api<CandidateDetail>(`/api/v1/admin/updates/${id}`)
    setDetail(d)
    setEffective(d.proposed_version?.effective_from ?? '')
    setPublished(d.proposed_version?.published_at ?? d.candidate.published_at?.slice(0, 10) ?? '')
  }, [])
  useEffect(() => {
    // A newly selected candidate must not display the previous candidate's review form.
    // oxlint-disable-next-line react/set-state-in-effect
    setDetail(null)
    setConfirmed(false)
    setAcknowledged(false)
    setPublished('')
    setEvidence('')
    setReason('')
    if (selected) loadDetail(selected).catch((e: Error) => flash(e.message, 'err'))
  }, [selected, loadDetail, flash])

  const act = async (work: () => Promise<string>) => {
    setBusy(true)
    try {
      flash(await work())
      await loadList()
      if (selected) await loadDetail(selected)
      onChanged()
    } catch (e) {
      flash(e instanceof ApiError && e.code === 'CONFLICTING_VERSION' ? 'يوجد إصدار معتمد آخر يبدأ تطبيقه في التاريخ نفسه.' : e instanceof Error ? e.message : 'تعذّر تنفيذ الإجراء.', 'err')
    } finally {
      setBusy(false)
    }
  }

  const sync = () =>
    act(async () => {
      const queued = await api<{ job_id: string }>('/api/v1/admin/updates/sync', { method: 'POST' })
      const job = await waitForJob(queued.job_id)
      if (job.status === 'failed') throw new Error(`تعذّر فحص المصادر: ${job.errors[job.errors.length - 1] ?? ''}`)
      const c = job.counts
      return `اكتمل الفحص: ${c.new ?? 0} عناصر جديدة، ${c.changed ?? 0} متغيّرة. تم تجاهل ${c.unchanged ?? 0} مكررة.`
    })

  const lastCheck = sources.map((s) => s.last_success_at).filter(Boolean).sort().pop()
  const failing = sources.find((s) => s.enabled && s.last_error)

  const c = detail?.candidate
  const version = detail?.proposed_version ?? null
  const warnings = c ? [...(version?.parse_warnings ?? []), ...(c.diff?.warnings ?? [])].filter((w) => BLOCKING.includes(w)) : []
  const rolledBack = version?.review_status === 'rolled_back'
  const stateKey = c ? (c.state === 'approved' && rolledBack ? 'rolled_back' : pillKey(c.state)) : 'pending'
  const statePill = pillOf(stateKey)
  const open = !!c && OPEN_STATES.includes(c.state)
  const canApprove = c?.state === 'needs_review'
  const approveReady = canApprove && confirmed && !!effective && evidence.trim().length >= 5 && (warnings.length === 0 || acknowledged)
  const lastDecision = detail?.review_events.filter((e) => e.decision !== 'classification_override').pop()

  const approve = () => {
    if (!c) return
    if (!effective) return flash('حدّد متى يبدأ تطبيق هذا النص. لا تستخدم تاريخ النشر إلا إذا نصّت الوثيقة على ذلك.', 'err')
    if (evidence.trim().length < 5) return flash('اذكر أين ورد تاريخ بدء التطبيق في المصدر الرسمي.', 'err')
    if (!confirmed) return flash('أكّد أنك راجعت تاريخ بدء التطبيق في النص الرسمي.', 'err')
    if (warnings.length > 0 && !acknowledged) return flash('أقرّ بالتحذيرات الظاهرة قبل الاعتماد.', 'err')
    act(async () => {
      await api(`/api/v1/admin/updates/${c.id}/approve`, {
        method: 'POST',
        body: { effective_from: effective, effective_date_evidence: evidence.trim(), acknowledge_warnings: acknowledged, ...(published ? { published_at: published } : {}), ...(reason.trim() ? { reason: reason.trim() } : {}) },
      })
      return effective > asOf ? `اعتُمد وأُعيدت الفهرسة. سيدخل في الإجابات اعتبارًا من ${effective}.` : 'اعتُمد وأُعيدت الفهرسة.'
    })
  }
  const reject = () => {
    if (!c) return
    if (reason.trim().length < 3) return flash('اكتب سبب الرفض.', 'err')
    act(async () => {
      await api(`/api/v1/admin/updates/${c.id}/reject`, { method: 'POST', body: { reason: reason.trim() } })
      return 'رُفض العنصر وسُجّل في سجل التدقيق.'
    })
  }
  const rollback = () => {
    if (!version) return
    act(async () => {
      await api(`/api/v1/admin/versions/${version.id}/rollback`, { method: 'POST', body: { reason: reason.trim() || 'تراجع عن الاعتماد من شاشة المراجعة' } })
      return 'أُلغي تفعيل الإصدار وأُعيد العمل بالنص السابق. سجل التدقيق محفوظ.'
    })
  }
  const reclassify = (classification: string) => {
    if (!c) return
    act(async () => {
      await api(`/api/v1/admin/updates/${c.id}`, { method: 'PATCH', body: { classification } })
      return 'حُدّث التصنيف.'
    })
  }

  const info = { padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 4 } as const

  return (
    <section className="updates-grid">
      <div style={{ minHeight: 0, overflowY: 'auto', borderInlineEnd: '1px solid #E4E0D6', background: '#F3F1EC', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '22px 18px 14px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h1 style={{ margin: 0, flex: 1, fontSize: 21, fontWeight: 700 }}>التحديثات</h1>
            <button onClick={sync} disabled={busy} style={{ whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 6, border: '1px solid #D9D4C8', background: '#fff', borderRadius: 8, padding: '6px 12px', fontSize: 12.5, fontWeight: 600, cursor: 'pointer', color: '#1F5F4A', opacity: busy ? 0.55 : 1 }}>
              <Icon d={ICON.refresh} size={14} />
              فحص الآن
            </button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: failing ? '#8A3A12' : '#5E655F' }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: failing ? '#C0602A' : lastCheck ? '#2E7D5B' : '#A8A69E', flex: 'none' }} />
            {failing ? `تعذّر فحص «${failing.name}»` : lastCheck ? `آخر فحص ناجح للمصادر: ${fmtDateTime(lastCheck)}` : 'لم يُفحص أي مصدر بعد'}
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '0 12px 20px' }}>
          {items?.length === 0 && <div style={{ padding: '8px 6px', fontSize: 13, color: '#5E655F' }}>لا توجد عناصر. اضغط «فحص الآن».</div>}
          {items?.map((u) => {
            const on = u.id === selected
            return (
              <button key={u.id} onClick={() => setSelected(u.id)} aria-pressed={on} style={{ textAlign: 'start', border: `1px solid ${on ? '#A9CDB9' : 'transparent'}`, background: on ? '#fff' : 'transparent', boxShadow: on ? '0 4px 14px -8px rgba(27,31,29,.25)' : 'none', borderRadius: 12, padding: '12px 14px', cursor: 'pointer', display: 'flex', flexDirection: 'column', gap: 6, color: '#1B1F1D' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <Pill k={pillKey(u.state)} size={11} style={{ gap: 5, padding: '1px 8px' }} />
                  {u.is_test_data && <TestBadge dashed />}
                  <span style={{ flex: 1 }} />
                  <span dir="ltr" className="mono" style={{ fontSize: 11, color: '#8C918C' }}>
                    {shortId(u.id)}
                  </span>
                </span>
                <span style={{ fontSize: 13.5, fontWeight: 600, lineHeight: 1.6, textWrap: 'pretty' }}>{u.title}</span>
                <span style={{ fontSize: 12, color: '#5E655F' }}>
                  {CLASS_AR[u.classification] ?? u.classification} · {u.source_name ?? 'رفع يدوي'}
                </span>
              </button>
            )
          })}
        </div>
      </div>

      <div className="pad-x" style={{ minHeight: 0, overflowY: 'auto', padding: '28px 36px 48px' }}>
        <div style={{ maxWidth: 920, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 20 }}>
          {!c && <Empty>{items?.length === 0 ? 'لا توجد تحديثات للمراجعة.' : 'جارٍ التحميل…'}</Empty>}
          {c && (
            <>
              <header style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
                  <Pill k={stateKey} />
                  <span style={{ fontSize: 12, fontWeight: 500, color: '#3F4541', background: '#F3F1EC', borderRadius: 6, padding: '2px 9px' }}>{CLASS_AR[c.classification] ?? c.classification}</span>
                  {c.is_test_data && <TestBadge dashed />}
                  <span dir="ltr" className="mono" style={{ fontSize: 12, color: '#8C918C' }}>
                    {shortId(c.id)}
                  </span>
                </div>
                <h2 style={{ margin: 0, fontSize: 23, fontWeight: 700, lineHeight: 1.5, textWrap: 'pretty' }}>{c.title}</h2>
                <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px 18px', fontSize: 13, color: '#5E655F' }}>
                  <span>{c.source_name ?? 'رفع يدوي'}</span>
                  <span>نُشر {fmtDate(c.published_at ?? version?.published_at)}</span>
                  {c.canonical_url && /^https?:\/\//.test(c.canonical_url) && (
                    <a href={c.canonical_url} target="_blank" rel="noopener noreferrer" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontWeight: 600 }}>
                      <Icon d={ICON.external} size={14} />
                      النص في المصدر الرسمي
                    </a>
                  )}
                </div>
                {c.summary && <p style={{ margin: 0, fontSize: 13.5, color: '#3F4541', lineHeight: 1.8 }}>{c.summary}</p>}
              </header>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 12 }}>
                <div className="card" style={info}>
                  <span style={{ fontSize: 12, color: '#5E655F' }}>الوثيقة المستهدفة</span>
                  <span style={{ fontSize: 15, fontWeight: 600 }}>{c.target_instrument_title_ar ?? '—'}</span>
                </div>
                <div className="card" style={info}>
                  <span style={{ fontSize: 12, color: '#5E655F' }}>المواد المتأثرة</span>
                  <span style={{ fontSize: 15, fontWeight: 600 }}>{c.affected_article_numbers.length ? c.affected_article_numbers.map((n) => `المادة ${n}`).join('، ') : 'لا يوجد'}</span>
                </div>
                <div style={{ ...info, background: '#F4F9F6', border: '1px solid #A9CDB9', borderRadius: 14 }}>
                  <span style={{ fontSize: 12, color: '#1F5F4A' }}>يبدأ تطبيقه في</span>
                  <span style={{ fontSize: 15, fontWeight: 700, color: '#14543C' }}>{version?.effective_from ? fmtDate(version.effective_from) : effective ? fmtDate(effective) : 'غير محدد'}</span>
                </div>
              </div>

              {c.error && (
                <div role="alert" style={{ fontSize: 13.5, color: '#8E2626', background: '#F8E3E1', border: '1px solid #E7C9C9', borderRadius: 14, padding: '12px 16px' }}>
                  تعذّر جلب النص الرسمي آليًا: <span dir="auto">{c.error}</span>. ارفع الوثيقة يدويًا من «المدونة النظامية».
                </div>
              )}

              <div style={{ background: version ? '#F4F9F6' : '#FFF8E8', border: `1px solid ${version ? '#A9CDB9' : '#E9C978'}`, borderRadius: 14, padding: '14px 16px', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12 }}>
                <span style={{ color: version ? '#1F5F4A' : '#8A5A00', display: 'flex' }}><Icon d={version ? ICON.check : ICON.info} size={19} /></span>
                <span style={{ flex: 1, minWidth: 240, fontSize: 13.5, lineHeight: 1.75 }}>
                  {version
                    ? 'النص الرسمي مرفق ومحفوظ كنسخة ثابتة. راجعه وحدد متى يبدأ تطبيقه وأين ورد ذلك، ثم أكّد المراجعة للاعتماد.'
                    : 'المعروض أعلاه ملخص رصد فقط، وليس نصًا قانونيًا صالحًا للاعتماد. أرفق ملف PDF الرسمي أو رابط صفحة النص الكامل من الجهة الرسمية.'}
                </span>
                {!version && <button type="button" onClick={() => setAttaching(true)} style={{ border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 9, padding: '9px 15px', fontSize: 13, fontWeight: 700, cursor: 'pointer' }}>إرفاق النص الرسمي</button>}
              </div>

              {c.diff && c.diff.changes.length > 0 ? (
                <div className="card" style={{ overflow: 'hidden' }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px 16px', padding: '14px 20px', borderBottom: '1px solid #EEEAE1' }}>
                    <h3 style={{ margin: 0, flex: 1, fontSize: 15.5, fontWeight: 700 }}>التغيير على النص</h3>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#5E655F' }}>
                      <span style={{ width: 12, height: 12, borderRadius: 3, background: '#F8E3E1' }} />
                      يُحذف
                    </span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#5E655F' }}>
                      <span style={{ width: 12, height: 12, borderRadius: 3, background: '#DCEDE3' }} />
                      يُضاف
                    </span>
                  </div>
                  {c.diff.changes.map((ch) => (
                    <DiffRow key={ch.article_number + ch.change} change={ch} />
                  ))}
                </div>
              ) : (
                <Empty>{version ? (c.diff ? 'هذا العنصر وثيقة جديدة أو لا يغيّر نص أي مادة في المدونة.' : 'لا توجد مقارنة متاحة.') : 'الاعتماد مقفل حتى إرفاق النص الرسمي الكامل.'}</Empty>
              )}

              {open && (
                <div className="card" style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: 16 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '10px 16px' }}>
                    <h3 style={{ margin: 0, flex: 1, fontSize: 15.5, fontWeight: 700 }}>قرار المراجعة</h3>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: '#5E655F' }}>
                      تصنيف عنصر الرصد
                      <select value={c.classification} disabled={busy} onChange={(e) => reclassify(e.target.value)} style={{ fontSize: 13, padding: '6px 10px', border: '1px solid #D9D4C8', borderRadius: 8, background: '#fff', color: '#1B1F1D' }}>
                        {Object.entries(CLASS_AR).map(([k, v]) => (
                          <option key={k} value={k}>
                            {v}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>

                  {canApprove && (
                    <>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(200px,1fr))', gap: 12 }}>
                        <label style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 }}>
                          متى نُشر النص رسميًا؟
                          <input className="field" type="date" value={published} onChange={(e) => setPublished(e.target.value)} />
                        </label>
                        <label style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 }}>
                          متى يبدأ تطبيق هذا النص؟
                          <input className="field" type="date" value={effective} onChange={(e) => setEffective(e.target.value)} />
                        </label>
                        <label style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 }}>
                          أين ورد تاريخ بدء التطبيق؟
                          <input className="field" value={evidence} onChange={(e) => setEvidence(e.target.value)} maxLength={2000} placeholder="مثال: نص القرار «يُعمل به من تاريخ نشره»، أو المادة ورقم الجريدة…" />
                        </label>
                      </div>
                      {warnings.length > 0 && (
                        <label style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: '14px 16px', borderRadius: 10, cursor: 'pointer', background: '#FBF5E6', border: '1px solid #F0DFB5' }}>
                          <input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} style={{ marginTop: 5, width: 16, height: 16, accentColor: '#1F5F4A', flex: 'none' }} />
                          <span style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 13.5, lineHeight: 1.7, color: '#5A4210' }}>
                            <span style={{ fontWeight: 600 }}>اطّلعتُ على التحذيرات التالية</span>
                            {warnings.map((w) => (
                              <span key={w}>{WARNING_AR[w]}</span>
                            ))}
                          </span>
                        </label>
                      )}
                      <label style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: '14px 16px', borderRadius: 10, cursor: 'pointer', background: confirmed ? '#F4F9F6' : '#FAF9F6', border: `1px solid ${confirmed ? '#A9CDB9' : '#E4E0D6'}` }}>
                        <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} style={{ marginTop: 5, width: 16, height: 16, accentColor: '#1F5F4A', flex: 'none' }} />
                        <span style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 13.5, lineHeight: 1.7 }}>
                          <span style={{ fontWeight: 600 }}>راجعتُ النص الرسمي وتأكدتُ من تاريخ بدء التطبيق</span>
                          <span style={{ color: '#5E655F' }}>نُشر النص في {published ? fmtDate(published) : '(غير محدد)'}، ويبدأ تطبيقه في {effective ? fmtDate(effective) : '(لم يُحدَّد بعد)'}. لا تستخدم تاريخ النشر كتاريخ للتطبيق إلا إذا نصّت الوثيقة على ذلك.</span>
                        </span>
                      </label>
                    </>
                  )}
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 }}>
                    <span>
                      ملاحظة <span style={{ fontWeight: 400, color: '#5E655F' }}>مطلوبة عند الرفض</span>
                    </span>
                    <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} placeholder="مثال: التعديل منشور كمشروع ولم يصدر بمرسوم بعد" style={{ fontSize: 13.5, border: '1px solid #D9D4C8', borderRadius: 10, padding: '10px 12px', resize: 'vertical', color: '#1B1F1D', background: '#fff' }} />
                  </label>
                  <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, paddingTop: 4 }}>
                    {canApprove && (
                      <button onClick={approve} disabled={busy} style={{ whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 8, border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 10, padding: '10px 20px', fontSize: 14, fontWeight: 600, cursor: 'pointer', opacity: approveReady && !busy ? 1 : 0.55 }}>
                        <Icon d={ICON.check} size={16} sw={2.2} stroke="#fff" />
                        اعتماد وإضافته للمدونة
                      </button>
                    )}
                    <button onClick={reject} disabled={busy} style={{ whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 8, border: '1px solid #E7C9C9', background: '#fff', color: '#8E2626', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 600, cursor: 'pointer' }}>
                      <Icon d={ICON.close} size={16} />
                      رفض
                    </button>
                    <span style={{ flex: 1, minWidth: 220, fontSize: 12, color: '#5E655F', lineHeight: 1.7 }}>بعد الاعتماد يُفهرس النص، لكنه لا يظهر في الإجابات قبل تاريخ نفاذه.</span>
                  </div>
                </div>
              )}

              {!open && (
                <div className="card" style={{ padding: '16px 20px', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, fontSize: 13.5 }}>
                  <span style={{ color: statePill.fg, display: 'flex' }}>
                    <Icon d={ICON.info} />
                  </span>
                  <span style={{ flex: 1, minWidth: 220, lineHeight: 1.7 }}>
                    {rolledBack
                      ? `أُلغي تفعيل هذا الإصدار في ${fmtDateTime(lastDecision?.decision_at)}. السبب: ${lastDecision?.reason ?? 'غير مذكور'}`
                      : c.state === 'approved'
                        ? `اعتُمد في ${fmtDateTime(lastDecision?.decision_at)}. ${version?.effective_from && version.effective_from > asOf ? `سيظهر في الإجابات اعتبارًا من ${fmtDate(version.effective_from)}، ولم تتغير الإجابات الحالية.` : 'النص نافذ ومُضاف إلى المدونة.'}`
                        : `رُفض في ${fmtDateTime(lastDecision?.decision_at)}. السبب: ${lastDecision?.reason ?? 'غير مذكور'}`}
                  </span>
                  {c.state === 'approved' && version?.index_active && (
                    <button onClick={rollback} disabled={busy} style={{ whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 10, padding: '8px 14px', fontSize: 13, fontWeight: 600, cursor: 'pointer', color: '#1B1F1D' }}>
                      التراجع عن الاعتماد
                    </button>
                  )}
                </div>
              )}
              {attaching && <AttachOfficialTextDialog candidate={c} flash={flash} onClose={() => setAttaching(false)} onDone={async () => { setAttaching(false); await loadList(); await loadDetail(c.id); onChanged() }} />}
            </>
          )}
        </div>
      </div>
    </section>
  )
}

function AttachOfficialTextDialog({ candidate, flash, onClose, onDone }: { candidate: Candidate; flash: Flash; onClose: () => void; onDone: () => void | Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const label = { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13, fontWeight: 500 } as const
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const raw = new FormData(e.currentTarget)
    const file = raw.get('file')
    const url = String(raw.get('register_url') ?? '').trim()
    const hasFile = file instanceof File && file.size > 0
    if (hasFile) raw.delete('register_url')
    else raw.delete('file')
    if (!hasFile && !url) return flash('اختر ملفًا أو أدخل رابطًا رسميًا.', 'err')
    raw.append('candidate_id', candidate.id)
    if (candidate.target_instrument_id) raw.append('instrument_id', candidate.target_instrument_id)
    if (!candidate.target_instrument_id) raw.append('title_ar', candidate.title)
    if (candidate.canonical_url) raw.append('source_url', candidate.canonical_url)
    setBusy(true)
    try {
      const queued = await api<{ job_id: string }>('/api/v1/admin/documents', { method: 'POST', form: raw })
      const job = await waitForJob(queued.job_id)
      if (job.status !== 'succeeded') throw new Error(job.errors[job.errors.length - 1] ?? 'تعذّر استخراج النص الرسمي.')
      flash(`أُرفق النص الرسمي واستُخرجت منه ${job.counts.articles ?? 0} مادة. راجعه الآن قبل الاعتماد.`)
      await onDone()
    } catch (err) {
      flash(err instanceof Error ? err.message : 'تعذّر إرفاق النص الرسمي.', 'err')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="attach-official-title" onKeyDown={(e) => e.key === 'Escape' && !busy && onClose()} style={{ position: 'fixed', inset: 0, background: 'rgba(27,31,29,.42)', display: 'grid', placeItems: 'center', zIndex: 40, padding: 16 }}>
      <form onSubmit={submit} className="card" style={{ width: 'min(620px,100%)', borderRadius: 16, padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 15 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}><h2 id="attach-official-title" style={{ margin: 0, flex: 1, fontSize: 19 }}>إرفاق النص الرسمي</h2><button type="button" disabled={busy} onClick={onClose} aria-label="إغلاق" style={{ border: 0, background: '#F3F1EC', borderRadius: 8, width: 32, height: 32, cursor: 'pointer', display: 'grid', placeItems: 'center' }}><Icon d={ICON.close} size={16} /></button></div>
        <p style={{ margin: 0, color: '#5E655F', fontSize: 13, lineHeight: 1.8 }}>استخدم أصل الوثيقة المنشور في هيئة الخبراء أو جريدة أم القرى. الخبر أو ملخص RSS لا يكفي للاعتماد.</p>
        <label style={label}>ملف النص الرسمي PDF أو HTML أو TXT<input className="field" type="file" name="file" accept=".pdf,.txt,.html,.htm,application/pdf,text/plain,text/html" /></label>
        <div style={{ textAlign: 'center', color: '#8C918C', fontSize: 12 }}>— أو —</div>
        <label style={label}>رابط صفحة النص الكامل في موقع رسمي<input className="field" dir="ltr" type="url" name="register_url" defaultValue={candidate.canonical_url ?? ''} /></label>
        <div style={{ background: '#F7F5F0', borderRadius: 9, padding: '10px 12px', fontSize: 12.5, lineHeight: 1.7 }}>بعد الاستخراج ستحدد متى يبدأ تطبيق النص وأين ورد ذلك، ثم تؤكد مراجعة النص الرسمي قبل الاعتماد.</div>
        <div style={{ display: 'flex', gap: 10 }}><button type="submit" disabled={busy} style={{ border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 700, cursor: busy ? 'wait' : 'pointer', opacity: busy ? .6 : 1 }}>{busy ? 'جارٍ جلب واستخراج النص…' : 'إرفاق واستخراج النص'}</button><button type="button" disabled={busy} onClick={onClose} style={{ border: '1px solid #D9D4C8', background: '#fff', borderRadius: 10, padding: '10px 18px', fontSize: 14, fontWeight: 600, cursor: 'pointer' }}>إلغاء</button></div>
      </form>
    </div>
  )
}

function DiffRow({ change }: { change: DiffChange }) {
  const d = inlineDiff(change.before ?? '', change.after ?? '')
  const p = { margin: 0, fontSize: 17, lineHeight: 2.15 }
  return (
    <div style={{ borderTop: '1px solid #EEEAE1' }}>
      <div style={{ padding: '10px 20px 0', fontSize: 12.5, fontWeight: 600, color: '#3F4541' }}>
        المادة {change.article_number} · {change.change === 'added' ? 'مادة مضافة' : change.change === 'removed' ? 'مادة محذوفة' : 'نص معدّل'}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(280px,1fr))' }}>
        <div style={{ padding: '10px 20px 20px', borderInlineEnd: '1px solid #EEEAE1' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#5E655F', marginBottom: 8 }}>النص الساري</div>
          {change.before ? (
            <p className="naskh" style={p}>
              {d.pre}
              {d.del && <span style={{ background: '#F8E3E1', color: '#7A2020', textDecoration: 'line-through', borderRadius: 3, padding: '0 3px' }}>{d.del}</span>}
              {d.post}
            </p>
          ) : (
            <p style={{ margin: 0, fontSize: 13, color: '#5E655F' }}>لا يوجد نص سابق.</p>
          )}
        </div>
        <div style={{ padding: '10px 20px 20px', background: '#FBFDFC' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#1F5F4A', marginBottom: 8 }}>النص بعد التعديل</div>
          {change.after ? (
            <p className="naskh" style={p}>
              {d.pre}
              {d.ins && <span style={{ background: '#DCEDE3', color: '#14543C', borderRadius: 3, padding: '0 3px', fontWeight: 600 }}>{d.ins}</span>}
              {d.post}
            </p>
          ) : (
            <p style={{ margin: 0, fontSize: 13, color: '#5E655F' }}>تُحذف المادة.</p>
          )}
        </div>
      </div>
    </div>
  )
}

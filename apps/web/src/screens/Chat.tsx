import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Article, ChatAnswer, Citation, Freshness, Law, Version } from '../api'
import { Icon, Logo, Pill } from '../ui'
import { DOT, ICON, fmtDate, fmtRiyadh, pillOf, shortId, todayIso } from '../uiData'
import type { ChatController } from '../useChat'

const DOMAINS: [string, string][] = [
  ['all', 'الكل'],
  ['labor', 'العمل'],
  ['commercial', 'الشركات'],
  ['data_protection', 'حماية البيانات'],
]
const SUGGESTIONS = ['ما الذي ينص عليه نظام العمل بشأن فترة التجربة؟', 'هل يجوز نقل البيانات الشخصية خارج المملكة؟', 'ما عقوبة التستر التجاري؟']
const PAGE_CHROME = new Set([
  'الرئيسية', 'القرارات', 'الديوان الملكي', 'قرارات مجلس الوزراء', 'قرارات وزارية', 'أوامر ملكية',
  'هيئة الخبراء بمجلس الوزراء', 'لوائح وأنظمة', 'مراسيم ملكية', 'هيئات', 'خريطة الموقع', 'تابعنا على',
  'اشترك في نشرتنا الإخبارية', 'Facebook', 'X', 'WhatsApp', 'نسخ الرابط', 'طباعة', 'تم نسخ الرابط',
  'تحميل...', 'نسخة تجريبية', 'مكة المكرمة',
])
const cleanDisplayedLegalText = (text: string) =>
  text
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !PAGE_CHROME.has(line) && !line.startsWith('جميع الحقوق محفوظة') && !line.startsWith('تاريخ آخر تعديل:') && !/^-+°C$/.test(line))
    .join('\n')
// Citations are rendered as a compact paragraph. Source-page line wrapping must
// never become artificial gaps in the legal text displayed to the user.
const compactSource = (text: string) => cleanDisplayedLegalText(text).replace(/\s+/g, ' ').trim()

function AnswerText({ text }: { text: string }) {
  const lines = cleanDisplayedLegalText(text).split('\n').map((line) => line.trim()).filter(Boolean)
  const blocks: { kind: 'text' | 'bullet' | 'number'; value: string }[] = lines.map((line) => {
    const bullet = line.match(/^[-•*]\s+(.+)$/)
    if (bullet) return { kind: 'bullet', value: bullet[1] }
    const numbered = line.match(/^\d+[.)-]\s+(.+)$/)
    if (numbered) return { kind: 'number', value: numbered[1] }
    return { kind: 'text', value: line }
  })
  const rendered: React.ReactNode[] = []
  for (let index = 0; index < blocks.length;) {
    const block = blocks[index]
    if (block.kind === 'text') {
      rendered.push(<p key={index} style={{ margin: rendered.length ? '10px 0 0' : 0 }}>{block.value}</p>)
      index += 1
      continue
    }
    const kind = block.kind
    const items: string[] = []
    while (index < blocks.length && blocks[index].kind === kind) {
      items.push(blocks[index].value)
      index += 1
    }
    const List = kind === 'number' ? 'ol' : 'ul'
    rendered.push(
      <List key={`${kind}-${index}`} style={{ margin: '10px 0 0', paddingInlineStart: 24, display: 'grid', gap: 6 }}>
        {items.map((item) => <li key={item}>{item}</li>)}
      </List>,
    )
  }
  return <div style={{ textAlign: 'start' }}>{rendered}</div>
}

/** Freshness contract (PRD v2 6.2): when the corpus was last verified and whether sources are current. */
function FreshnessBar({ freshness }: { freshness: Freshness }) {
  const healthy = freshness.source_health === 'healthy'
  const palette = healthy
    ? { background: '#F3F8F5', border: '#DCE9E1', accent: '#1F5F4A', muted: '#5E655F' }
    : { background: '#FFF8E8', border: '#EBD9A8', accent: '#7A4F00', muted: '#685A3B' }
  return (
    <div className="pad-x" role="status" style={{ padding: '10px 28px', color: '#303632', background: palette.background, borderBottom: `1px solid ${palette.border}` }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px 18px', fontSize: 12.5, lineHeight: 1.55 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span aria-hidden="true" style={{ display: 'flex', color: palette.accent }}><Icon d={healthy ? ICON.check : ICON.warn} size={17} /></span>
          <strong style={{ fontSize: 13 }}>حالة المصادر</strong>
          <Pill k={freshness.source_health} size={11.5} />
        </div>
        <span style={{ color: palette.muted }}>
          آخر تحقق: <strong style={{ color: '#303632' }}>{freshness.corpus_verified_at ? fmtRiyadh(freshness.corpus_verified_at) : 'لم يتم بعد'}</strong>
        </span>
        {freshness.latest_included_publication && (
          <span style={{ color: palette.muted }}>أحدث نشر مضمّن: <strong style={{ color: '#303632' }}>{fmtDate(freshness.latest_included_publication)}</strong></span>
        )}
        <span style={{ color: palette.muted }}>المزامنة القادمة: <strong style={{ color: '#303632' }}>{fmtRiyadh(freshness.next_scheduled_sync)}</strong></span>
      </div>
      {freshness.warning_ar && (
        <div style={{ marginTop: 7, paddingTop: 7, borderTop: `1px solid ${palette.border}`, display: 'flex', alignItems: 'flex-start', gap: 7, color: palette.accent, fontSize: 12.5, lineHeight: 1.55 }}>
          <span aria-hidden="true" style={{ display: 'flex', marginTop: 1 }}><Icon d={ICON.info} size={15} /></span>
          <span>{freshness.warning_ar}</span>
        </div>
      )}
    </div>
  )
}

export function Chat({ chat, freshness }: { chat: ChatController; freshness: Freshness | null }) {
  const { messages, domain, setDomain, asOf, setAsOf, input, setInput, source, setSource, send, clear, busy } = chat
  const [wide, setWide] = useState(() => window.innerWidth >= 1200)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= 1200)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' }) // newer browsers return a Promise here
  }, [messages])

  const canSend = !!input.trim() && !busy

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: source && wide ? 'minmax(0,1fr) minmax(320px,420px)' : 'minmax(0,1fr)', position: 'relative' }}>
      <section style={{ minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <header className="pad-x" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '12px 20px', padding: '14px 28px', borderBottom: '1px solid #E4E0D6', background: '#F6F4EF' }}>
          <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.35 }}>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 700, whiteSpace: 'nowrap' }}>اسأل عن نظام</h1>
            <span style={{ fontSize: 12, color: '#5E655F', whiteSpace: 'nowrap' }}>إجابات من النصوص الرسمية المعتمدة فقط</span>
          </div>
          <div style={{ flex: 1 }} />
          {messages.length > 0 && (
            <button type="button" onClick={clear} disabled={busy} style={{ whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', color: '#3F4541', borderRadius: 9, padding: '6px 12px', fontSize: 12.5, fontWeight: 600, cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.55 : 1 }}>
              محادثة جديدة
            </button>
          )}
          <div role="group" aria-label="المجال" style={{ display: 'flex', gap: 2, background: '#ECE8DF', padding: 3, borderRadius: 10 }}>
            {DOMAINS.map(([key, label]) => {
              const on = domain === key
              return (
                <button key={key} onClick={() => setDomain(key)} aria-pressed={on} style={{ whiteSpace: 'nowrap', border: 0, borderRadius: 8, padding: '6px 14px', fontSize: 13, fontWeight: on ? 600 : 500, cursor: 'pointer', background: on ? '#fff' : 'transparent', color: on ? '#1B1F1D' : '#5E655F', boxShadow: on ? '0 1px 2px rgba(0,0,0,.08)' : 'none' }}>
                  {label}
                </button>
              )
            })}
          </div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, background: '#fff', border: '1px solid #D9D4C8', borderRadius: 10, padding: '4px 6px 4px 12px', fontSize: 13, color: '#5E655F', whiteSpace: 'nowrap' }}>
            <span style={{ color: '#1F5F4A', display: 'flex' }}>
              <Icon d={ICON.calendar} size={16} />
            </span>
            النص النافذ في
            <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value || todayIso())} style={{ fontSize: 13, fontWeight: 600, padding: '3px 4px', border: 0, background: 'transparent', color: '#1B1F1D', outline: 'none' }} />
          </label>
        </header>
        {freshness && <FreshnessBar freshness={freshness} />}

        <div className="pad-x" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '32px 28px 24px' }}>
          <div aria-live="polite" style={{ maxWidth: 780, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 28 }}>
            {messages.length === 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12, padding: '56px 0 8px', textAlign: 'center', color: '#5E655F' }}>
                <Logo size={44} />
                <div style={{ fontSize: 17, fontWeight: 700, color: '#1B1F1D' }}>اسأل عن حكم في الأنظمة المفهرسة</div>
                <div style={{ fontSize: 13.5, maxWidth: 440, lineHeight: 1.8 }}>كل إجابة تُعرض مع المادة التي استندت إليها وتاريخ نفاذها. إذا لم يوجد نص معتمد كافٍ، يمتنع المساعد عن الإجابة.</div>
              </div>
            )}
            {messages.map((m, i) => {
              if (m.role === 'user')
                return (
                  <div key={i} style={{ alignSelf: 'flex-end', maxWidth: '78%', background: '#1F5F4A', color: '#fff', padding: '11px 18px', borderRadius: '18px 18px 18px 6px', fontSize: 15, lineHeight: 1.7, boxShadow: '0 2px 6px rgba(31,95,74,.18)', whiteSpace: 'pre-wrap' }}>
                    {m.text}
                  </div>
                )
              if (m.role === 'pending')
                return (
                  <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                    <Logo size={30} />
                    <div style={{ background: '#fff', border: '1px solid #E4E0D6', borderRadius: 14, padding: '14px 18px', color: '#5E655F', fontSize: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
                      <span className="dots" style={{ display: 'flex', gap: 4 }}>
                        <span />
                        <span />
                        <span />
                      </span>
                      جارٍ البحث في النصوص النافذة في {fmtDate(m.asOf)}…
                    </div>
                  </div>
                )
              if (m.role === 'streaming')
                return (
                  <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                    <Logo size={30} />
                    <article style={{ flex: 1, minWidth: 0, background: '#fff', border: '1px solid #E4E0D6', borderRadius: 16, padding: '16px 22px', boxShadow: '0 1px 2px rgba(27,31,29,.04)', fontSize: 16, lineHeight: 1.9, whiteSpace: 'pre-wrap' }}>
                      {m.text}
                      <span aria-label="جارٍ إكمال الإجابة" style={{ display: 'inline-block', width: 7, height: '1.1em', marginInlineStart: 4, background: '#1F5F4A', verticalAlign: 'text-bottom', animation: 'pulse 1s ease-in-out infinite' }} />
                    </article>
                  </div>
                )
              if (m.role === 'error')
                return (
                  <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                    <Logo size={30} />
                    <div role="alert" style={{ background: '#F8E3E1', border: '1px solid #E7C9C9', borderRadius: 14, padding: '12px 18px', color: '#8E2626', fontSize: 14 }}>
                      {m.text} لم تُقدَّم أي إجابة.
                    </div>
                  </div>
                )
              return <Answer key={i} answer={m.answer} latencyMs={m.latencyMs} selected={source} onOpen={setSource} />
            })}
            <div ref={endRef} />
          </div>
        </div>

        <div className="pad-x" style={{ padding: '8px 28px 18px', background: 'linear-gradient(180deg,rgba(246,244,239,0),#F6F4EF 30%)' }}>
          <div style={{ maxWidth: 780, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div className="chips">
              {SUGGESTIONS.map((text) => (
                <button key={text} onClick={() => send(text)} disabled={busy} style={{ flex: 'none', whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 6, border: '1px solid #E1DCD0', background: '#fff', borderRadius: 20, padding: '6px 12px', fontSize: 13, color: '#3F4541', cursor: 'pointer' }}>
                  <span style={{ color: '#B08A2E', display: 'flex' }}>
                    <Icon d={ICON.spark} size={14} />
                  </span>
                  {text}
                </button>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', background: '#fff', border: '1px solid #D9D4C8', borderRadius: 16, padding: 10, boxShadow: '0 10px 30px -14px rgba(27,31,29,.25)' }}>
              <textarea
                aria-label="سؤالك"
                rows={2}
                maxLength={1000}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault()
                    send()
                  }
                }}
                placeholder="اكتب سؤالك بالعربية، مثل: ما مدة فترة التجربة في نظام العمل؟"
                style={{ flex: 1, resize: 'none', border: 0, outline: 'none', fontSize: 15, lineHeight: 1.7, padding: '4px 8px', color: '#1B1F1D', background: 'transparent' }}
              />
              <button onClick={() => send()} disabled={!canSend} aria-label="إرسال" style={{ flex: 'none', width: 42, height: 42, border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 12, display: 'grid', placeItems: 'center', cursor: 'pointer', opacity: canSend ? 1 : 0.45 }}>
                <Icon d={ICON.send} size={19} sw={2} stroke="#fff" />
              </button>
            </div>
            <div style={{ fontSize: 11.5, color: '#5E655F', textAlign: 'center' }}>Enter للإرسال · Shift + Enter لسطر جديد · يمتنع المساعد عن الإجابة إذا لم يجد مصدرًا كافيًا</div>
          </div>
        </div>
      </section>

      {source && <SourcePanel key={source.article_id} citation={source} asOf={asOf} wide={wide} onClose={() => setSource(null)} />}
    </div>
  )
}

function Answer({ answer, latencyMs, selected, onOpen }: { answer: ChatAnswer; latencyMs: number; selected: Citation | null; onOpen: (c: Citation) => void }) {
  return (
    <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
      <Logo size={30} />
      <article style={{ flex: 1, minWidth: 0, background: '#fff', border: '1px solid #E4E0D6', borderRadius: 16, boxShadow: '0 1px 2px rgba(27,31,29,.04),0 8px 24px -12px rgba(27,31,29,.10)', overflow: 'hidden' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, padding: '14px 22px 0' }}>
          <Pill k={answer.answer_status} style={{ padding: '3px 10px' }} />
          <span style={{ whiteSpace: 'nowrap', fontSize: 12.5, color: '#5E655F' }}>
            وفق النص النافذ في {fmtDate(answer.as_of_date)}
          </span>
        </div>
        <div style={{ padding: '14px 22px 4px', fontSize: 16, lineHeight: 1.9, textWrap: 'pretty' }}>
          <AnswerText text={answer.answer_ar} />
          {answer.citations.length > 0 && (
            <span aria-label="مصادر الإجابة" style={{ display: 'inline-flex', gap: 5, marginTop: 10 }}>
              {answer.citations.map((c, i) => (
                <button key={c.article_id} onClick={() => onOpen(c)} aria-label={`فتح المصدر ${i + 1}`} style={{ whiteSpace: 'nowrap', border: 0, background: '#1F5F4A', color: '#fff', fontSize: 10.5, fontWeight: 700, borderRadius: '50%', width: 20, height: 20, lineHeight: '20px', padding: 0, cursor: 'pointer', display: 'inline-block', textAlign: 'center' }}>
                  {i + 1}
                </button>
              ))}
            </span>
          )}
        </div>
        {answer.citations.length > 0 && (
          <div style={{ padding: '14px 22px 2px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, fontWeight: 600, color: '#3F4541', marginBottom: 10 }}>
              <span style={{ color: '#1F5F4A', display: 'flex' }}>
                <Icon d={ICON.openBook} size={16} />
              </span>
              الأساس النظامي
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {answer.citations.map((c, i) => {
                const on = selected?.article_id === c.article_id
                const fullDecisionUrl = c.official_url ?? c.source_url
                return (
                  <div key={c.article_id} style={{ display: 'flex', alignItems: 'stretch', border: `1px solid ${on ? '#A9CDB9' : '#E4E0D6'}`, background: on ? '#F3F8F5' : '#fff', borderRadius: 12, overflow: 'hidden' }}>
                    <button onClick={() => onOpen(c)} style={{ display: 'flex', alignItems: 'center', gap: 14, flex: 1, minWidth: 0, textAlign: 'start', border: 0, background: 'transparent', padding: '12px 14px', cursor: 'pointer', color: '#1B1F1D' }}>
                      <span style={{ width: 28, height: 28, borderRadius: 8, background: '#1F5F4A', color: '#fff', display: 'grid', placeItems: 'center', fontSize: 13, fontWeight: 700, flex: 'none' }}>{i + 1}</span>
                      <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', lineHeight: 1.45 }}>
                        <span style={{ fontWeight: 600, fontSize: 14.5 }}>
                          {c.law_title_ar} — {c.article_label ?? 'نص الوثيقة'}
                        </span>
                        <span style={{ fontSize: 12.5, color: '#5E655F' }}>
                          سارية منذ {fmtDate(c.effective_from)}
                          {c.effective_to ? ` · حتى ${fmtDate(c.effective_to)}` : ''}
                          {c.is_test_data ? ' · بيانات تجريبية' : ''}
                        </span>
                      </span>
                      <Pill k={c.legal_status} size={11.5} dot={false} />
                      <span style={{ color: '#5E655F', display: 'flex' }}><Icon d={ICON.chevron} /></span>
                    </button>
                    {fullDecisionUrl && (
                      <a href={fullDecisionUrl} target="_blank" rel="noopener noreferrer" aria-label={`فتح القرار كاملًا ${i + 1}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 5, alignSelf: 'center', marginInlineEnd: 12, whiteSpace: 'nowrap', color: '#1F5F4A', fontSize: 12, fontWeight: 700, textDecoration: 'none' }}>
                        القرار كاملًا <Icon d={ICON.external} size={14} />
                      </a>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}
        {(answer.related_services?.length ?? 0) > 0 && (
          <section aria-label="خدمات حكومية ذات صلة" style={{ padding: '14px 22px 2px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, fontWeight: 600, color: '#3F4541', marginBottom: 10 }}>
              <span style={{ color: '#1F5F4A', display: 'flex' }}><Icon d={ICON.external} size={16} /></span>
              خدمات حكومية ذات صلة
              <span style={{ color: '#7A807B', fontWeight: 400 }}>— ليست سندًا للإجابة القانونية</span>
            </div>
            <div style={{ display: 'grid', gap: 8 }}>
              {answer.related_services?.map((service) => (
                <div key={service.id} style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, border: '1px solid #BFD8C9', background: '#F3F8F5', borderRadius: 12, padding: '12px 14px', color: '#1B1F1D' }}>
                  <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
                    <span style={{ fontSize: 14.5, fontWeight: 700, color: '#17543D' }}>{service.title_ar}</span>
                    <span style={{ fontSize: 12.5, lineHeight: 1.7, color: '#4F5752' }}>{service.guidance_ar}</span>
                    <span style={{ fontSize: 11.5, color: '#737A75' }}>{service.provider_ar} · رابط رسمي · تحقق {fmtDate(service.last_verified_at)}</span>
                  </span>
                  <span style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 6 }}>
                    <a href={service.official_info_url} target="_blank" rel="noopener noreferrer" style={{ whiteSpace: 'nowrap', color: '#1F5F4A', fontSize: 12, fontWeight: 600 }}>التفاصيل الرسمية</a>
                    <a href={service.official_url} target="_blank" rel="noopener noreferrer" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', color: '#fff', background: '#1F5F4A', borderRadius: 8, padding: '7px 10px', fontSize: 12.5, fontWeight: 700, textDecoration: 'none' }}>
                      فتح الخدمة <Icon d={ICON.external} size={14} />
                    </a>
                  </span>
                </div>
              ))}
            </div>
          </section>
        )}
        {answer.caveats.length > 0 && (
          <div style={{ margin: '14px 22px 0', padding: '12px 14px', background: '#FBF5E6', border: '1px solid #F0DFB5', borderRadius: 12, display: 'flex', gap: 10, alignItems: 'flex-start' }}>
            <span style={{ color: '#8A5A00', display: 'flex', marginTop: 3 }}>
              <Icon d={ICON.info} size={17} />
            </span>
            <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4 }}>
              {answer.caveats.map((cv) => (
                <li key={cv} style={{ fontSize: 13.5, color: '#5A4210', lineHeight: 1.7 }}>
                  {cv}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div style={{ marginTop: 16, padding: '11px 22px', borderTop: '1px solid #EEEAE1', background: '#FAF9F6', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px 16px', fontSize: 12, color: '#5E655F' }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1, minWidth: 240 }}>
            <Icon d={ICON.shield} size={14} />
            {answer.disclaimer_ar}
          </span>
          {answer.source_health && (
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexBasis: '100%' }}>
              <Icon d={ICON.clock} size={14} />
              مصادر مختارة وليست شاملة · آخر تحقق من المصادر: {answer.corpus_verified_at ? fmtRiyadh(answer.corpus_verified_at) : 'لم يتم بعد'}
              <Pill k={answer.source_health} size={10.5} dot={false} style={{ padding: '0 7px' }} />
            </span>
          )}
          <span dir="ltr" className="mono" title="معرّف الطلب وزمن الاستجابة" style={{ fontSize: 11, color: '#8C918C' }}>
            trace-{shortId(answer.trace_id)} · {(latencyMs / 1000).toFixed(1)}s
          </span>
        </div>
      </article>
    </div>
  )
}

type ArticleDetail = { article: Article; version: Version; law: Law }

function SourcePanel({ citation, asOf, wide, onClose }: { citation: Citation; asOf: string; wide: boolean; onClose: () => void }) {
  const [detail, setDetail] = useState<ArticleDetail | null>(null)
  const [versions, setVersions] = useState<Version[]>([])

  useEffect(() => {
    let live = true
    api<ArticleDetail>(`/api/v1/articles/${citation.article_id}`)
      .then((d) => live && setDetail(d))
      .catch(() => undefined)
    api<{ versions: Version[] }>(`/api/v1/laws/${citation.law_id}/versions?as_of_date=${asOf}`)
      .then((d) => live && setVersions([...d.versions].reverse()))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [citation.article_id, citation.law_id, asOf])

  const cell = { background: '#FAF9F6', padding: '10px 12px' }
  const cellLabel = { color: '#5E655F', marginBottom: 3 }
  const cellValue = { fontSize: 13, fontWeight: 600 }
  const version = detail?.version
  const status = pillOf(citation.legal_status)

  return (
    <aside aria-label="نص المادة" style={{ minHeight: 0, overflowY: 'auto', background: '#fff', borderInlineStart: '1px solid #E4E0D6', display: 'flex', flexDirection: 'column', position: wide ? 'static' : 'absolute', insetBlock: 0, insetInlineEnd: 0, width: wide ? 'auto' : 'min(420px, 92%)', boxShadow: wide ? 'none' : '0 0 40px rgba(0,0,0,.18)', zIndex: 5 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '18px 20px', borderBottom: '1px solid #EEEAE1', position: 'sticky', top: 0, background: '#fff', zIndex: 1 }}>
        <span style={{ width: 40, height: 40, borderRadius: 10, background: '#E6EFEA', color: '#1F5F4A', display: 'grid', placeItems: 'center', flex: 'none' }}>
          <Icon d={ICON.openBook} size={20} />
        </span>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', lineHeight: 1.4 }}>
          <span style={{ fontSize: 17, fontWeight: 700 }}>{citation.law_title_ar}</span>
          <span style={{ fontSize: 14, fontWeight: 600, color: '#1F5F4A' }}>{citation.article_label ?? 'نص الوثيقة'}</span>
          <span style={{ fontSize: 12, color: '#5E655F', marginTop: 2 }}>{version?.source_authority ?? detail?.law.issuing_authority ?? citation.chapter ?? ''}</span>
        </div>
        <button onClick={onClose} aria-label="إغلاق" style={{ flex: 'none', border: 0, background: '#F3F1EC', borderRadius: 8, width: 32, height: 32, cursor: 'pointer', color: '#3F4541', display: 'grid', placeItems: 'center' }}>
          <Icon d={ICON.close} size={16} />
        </button>
      </div>
      <div style={{ padding: '16px 20px 0' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1, background: '#EEEAE1', border: '1px solid #EEEAE1', borderRadius: 12, overflow: 'hidden', fontSize: 12 }}>
          <div style={cell}>
            <div style={cellLabel}>الحالة</div>
            <span style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center', gap: 5, background: status.bg, color: status.fg, fontWeight: 600, padding: '1px 9px', borderRadius: 20 }}>
              <span style={{ width: 5, height: 5, borderRadius: '50%', background: status.fg }} />
              {status.label}
            </span>
          </div>
          <div style={cell}>
            <div style={cellLabel}>النسخة</div>
            <div style={cellValue}>{version?.version_label ?? '—'}</div>
          </div>
          <div style={cell}>
            <div style={cellLabel}>تاريخ النشر</div>
            <div style={cellValue}>{fmtDate(version?.published_at)}</div>
          </div>
          <div style={cell}>
            <div style={cellLabel}>تاريخ النفاذ</div>
            <div style={cellValue}>{fmtDate(citation.effective_from)}</div>
          </div>
        </div>
      </div>
      <div style={{ padding: '20px 20px 8px' }}>
        <div style={{ fontSize: 12.5, fontWeight: 600, color: '#3F4541', marginBottom: 8 }}>نص المادة</div>
        {/* The API cites whole articles, so no sub-span is highlighted. */}
        <div className="naskh" style={{ fontSize: 17.5, lineHeight: 1.95, color: '#1B1F1D', whiteSpace: 'pre-line' }}>
          {compactSource(detail?.article.original_text_ar ?? citation.quote)}
        </div>
      </div>
      {versions.length > 0 && (
        <div style={{ padding: '16px 20px 20px' }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, color: '#3F4541', marginBottom: 10 }}>سجل الإصدارات</div>
          <ol style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column' }}>
            {versions.map((v, i) => {
              const dot = DOT[v.legal_status] ?? '#A8A69E'
              return (
                <li key={v.id} style={{ display: 'flex', gap: 12, alignItems: 'stretch' }}>
                  <span style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', width: 12, flex: 'none' }}>
                    <span style={{ width: 10, height: 10, borderRadius: '50%', background: dot, marginTop: 6, boxShadow: `0 0 0 3px #fff,0 0 0 4px ${dot}` }} />
                    <span style={{ flex: 1, width: 2, background: '#E4E0D6', marginTop: 4, opacity: i === versions.length - 1 ? 0 : 1 }} />
                  </span>
                  <span style={{ flex: 1, display: 'flex', alignItems: 'flex-start', gap: 8, paddingBottom: 14 }}>
                    <span style={{ flex: 1, fontSize: 13, lineHeight: 1.5 }}>
                      {v.version_label ?? 'إصدار'} — نفاذ {fmtDate(v.effective_from)}
                      {v.id === citation.version_id ? ' (المستشهد به)' : ''}
                    </span>
                    <Pill k={v.legal_status} size={11} dot={false} style={{ padding: '1px 8px' }} />
                  </span>
                </li>
              )
            })}
          </ol>
        </div>
      )}
      <div style={{ marginTop: 'auto', padding: '14px 20px 18px', borderTop: '1px solid #EEEAE1', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {(citation.official_url ?? citation.source_url) && (
          <a href={citation.official_url ?? citation.source_url ?? undefined} target="_blank" rel="noopener noreferrer" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, border: '1px solid #1F5F4A', borderRadius: 10, padding: '9px 14px', fontSize: 13.5, fontWeight: 600, color: '#1F5F4A', textDecoration: 'none' }}>
            <Icon d={ICON.external} size={16} />
            {citation.is_test_data ? 'فتح المصدر (رابط تجريبي)' : 'فتح القرار كاملًا من المصدر الرسمي'}
          </a>
        )}
        {version && (
          <span dir="ltr" className="mono" title="بصمة النسخة" style={{ fontSize: 10.5, color: '#8C918C', textAlign: 'center' }}>
            sha256 {version.raw_hash.slice(0, 4)}…{version.raw_hash.slice(-4)}
          </span>
        )}
      </div>
    </aside>
  )
}

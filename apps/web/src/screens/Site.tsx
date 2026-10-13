// Landing page and full-page login, from the design file "Saudi Legal AI Site.dc.html".
import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { api } from '../api'
import type { Law, Page, Version } from '../api'
import { Icon, Logo } from '../ui'
import { DOMAIN_AR, ICON, fmtDate, todayIso } from '../uiData'

export const SERIF = "'Noto Naskh Arabic',serif"
const WRAP: CSSProperties = { maxWidth: 1160, margin: '0 auto' }

const STEPS = [
  { n: '01', icon: ICON.chat, title: 'اسأل بالعربية', body: 'اكتب سؤالك كما تقوله، واختر المجال وتاريخًا محددًا إن أردت معرفة النص النافذ في وقت سابق أو لاحق.' },
  { n: '02', icon: ICON.openBook, title: 'راجع المادة بنفسك', body: 'كل إجابة مربوطة بالمادة التي تستند إليها. افتح النص الكامل والإصدار ورابط المصدر.' },
  { n: '03', icon: 'M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0', title: 'تابع التعديلات', body: 'يراقب النظام المصادر المعتمدة، ولا يدخل أي تعديل في الإجابات إلا بعد مراجعته وحلول تاريخ نفاذه.' },
]
const PRINCIPLES = [
  { icon: 'M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71', title: 'لا إجابة دون مصدر', body: 'كل حكم نظامي مرتبط بمادة مسترجعة من نص معتمد. الاستشهادات تُتحقق آليًا قبل عرضها.' },
  { icon: ICON.calendar, title: 'التاريخ جزء من الإجابة', body: 'يفرّق المساعد بين تاريخ النشر وتاريخ النفاذ، ولا يقدّم نصًا مستقبليًا على أنه سارٍ اليوم.' },
  { icon: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4', title: 'مراجعة قبل النشر', body: 'التحديثات المكتشفة تبقى خارج الإجابات حتى يعتمدها مراجع بعد التحقق من الجهة والتاريخ.' },
  { icon: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 8v4M12 16h.01', title: 'الامتناع عند الشك', body: 'إذا كانت الأدلة ناقصة أو متعارضة أو خارج التغطية، يقول المساعد ذلك صراحة بدل التخمين.' },
]
const DOMAIN_ICON: Record<string, string> = {
  labor: 'M16 20V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16M4 6h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z',
  commercial: 'M3 21h18M5 21V7l7-4 7 4v14M9 9h1M14 9h1M9 13h1M14 13h1M9 17h1M14 17h1',
  data_protection: ICON.lock,
}

/** Photo slot: shows the design's image when it exists in public/assets, a plain tint otherwise. */
export function Photo({ src, alt }: { src: string; alt: string }) {
  const [ok, setOk] = useState(true)
  return ok ? <img src={src} alt={alt} onError={() => setOk(false)} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} /> : null
}

export function Landing({ onTry, onLogin, onSignup }: { onTry: () => void; onLogin: () => void; onSignup: () => void }) {
  const [laws, setLaws] = useState<Law[] | null>(null)
  const [sample, setSample] = useState<{ law: string; article: string; text: string; effective: string | null } | null>(null)
  useEffect(() => {
    api<Page<Law>>('/api/v1/laws?limit=100')
      .then(async (p) => {
        setLaws(p.items)
        // Pick a short article from a version that is in force today.
        for (const law of p.items) {
          const { versions } = await api<{ versions: Version[] }>(`/api/v1/laws/${law.id}/versions`)
          const active = versions.find((v) => v.legal_status === 'active')
          if (!active) continue
          const { articles } = await api<{ articles: { article_number: string | null; article_label: string | null; original_text_ar: string }[] }>(`/api/v1/versions/${active.id}/articles`)
          const pick = articles.find((a) => a.article_number && a.article_label && a.original_text_ar.length >= 60 && a.original_text_ar.length <= 320)
          if (pick) return setSample({ law: law.official_title_ar, article: pick.article_label!, text: pick.original_text_ar, effective: active.effective_from })
        }
      })
      .catch(() => setLaws((cur) => cur ?? []))
  }, [])

  const domains = [...new Set((laws ?? []).map((l) => l.domain))]
  const anyTest = (laws ?? []).some((l) => l.is_test_data)
  const primary: CSSProperties = { whiteSpace: 'nowrap', background: '#1F5F4A', color: '#fff', border: 0, borderRadius: 10, padding: '12px 22px', fontSize: 15, fontWeight: 600, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 8 }
  const h2: CSSProperties = { margin: 0, fontFamily: SERIF, fontSize: 32, fontWeight: 700 }

  return (
    <div dir="rtl" lang="ar" style={{ fontFamily: "'IBM Plex Sans Arabic',sans-serif", color: '#1B1F1D', background: '#F6F4EF', minHeight: '100vh', fontSize: 15, lineHeight: 1.7 }}>
      <header style={{ position: 'sticky', top: 0, zIndex: 10, background: 'rgba(246,244,239,.92)', backdropFilter: 'blur(8px)', borderBottom: '1px solid #E4E0D6' }}>
        <div style={{ ...WRAP, padding: '14px 28px', display: 'flex', alignItems: 'center', gap: 28, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Logo size={32} />
            <span style={{ fontWeight: 700, fontSize: 16, whiteSpace: 'nowrap' }}>المساعد القانوني</span>
          </div>
          <nav aria-label="أقسام الصفحة" style={{ display: 'flex', gap: 22, fontSize: 14, flexWrap: 'wrap' }}>
            <a href="#how" style={{ color: '#3F4541' }}>كيف يعمل</a>
            <a href="#trust" style={{ color: '#3F4541' }}>مبادئ الموثوقية</a>
            <a href="#coverage" style={{ color: '#3F4541' }}>التغطية</a>
          </nav>
          <div style={{ flex: 1 }} />
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={onLogin} style={{ whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 8, padding: '7px 16px', fontSize: 14, fontWeight: 500, cursor: 'pointer', color: '#1B1F1D' }}>تسجيل الدخول</button>
            <button onClick={onSignup} style={{ whiteSpace: 'nowrap', border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 8, padding: '7px 16px', fontSize: 14, fontWeight: 600, cursor: 'pointer' }}>إنشاء حساب</button>
          </div>
        </div>
      </header>

      <section style={{ ...WRAP, padding: '72px 28px 80px', display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,440px),1fr))', gap: 56, alignItems: 'center' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
          <span style={{ alignSelf: 'flex-start', fontSize: 12.5, fontWeight: 600, color: '#1F5F4A', background: '#E6EFEA', borderRadius: 20, padding: '3px 12px', whiteSpace: 'nowrap' }}>للأنظمة السعودية</span>
          <h1 style={{ margin: 0, fontFamily: SERIF, fontSize: 'clamp(36px,4.6vw,56px)', lineHeight: 1.35, fontWeight: 700, textWrap: 'balance' }}>إجابات من النص النظامي الرسمي، بالمادة والتاريخ.</h1>
          <p style={{ margin: 0, fontSize: 17, color: '#3F4541', maxWidth: 520, textWrap: 'pretty' }}>اسأل عن نظام العمل أو الشركات أو حماية البيانات الشخصية. يستشهد المساعد بالمادة والإصدار النافذ، ويمتنع عن الإجابة عندما لا يجد مصدرًا معتمدًا.</p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            <button onClick={onTry} style={primary}>
              جرّب دون تسجيل
              <Icon d={ICON.send} stroke="#fff" />
            </button>
            <button onClick={onSignup} style={{ whiteSpace: 'nowrap', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 10, padding: '12px 22px', fontSize: 15, fontWeight: 600, cursor: 'pointer', color: '#1B1F1D' }}>إنشاء حساب مجاني</button>
          </div>
          <span style={{ fontSize: 12.5, color: '#5E655F' }}>خدمة معلومات قانونية عامة، وليست استشارة قانونية.</span>
        </div>

        <div style={{ position: 'relative', minHeight: 520, display: 'flex', alignItems: 'flex-end' }}>
          <div style={{ position: 'absolute', top: 0, bottom: 56, insetInlineEnd: 0, width: '78%', borderRadius: 24, overflow: 'hidden', background: '#D9D4C8' }}>
            <Photo src="/assets/riyadh-day.jpg" alt="أفق مدينة الرياض" />
            <div style={{ position: 'absolute', inset: 0, background: 'linear-gradient(180deg,rgba(31,95,74,0) 40%,rgba(27,31,29,.35) 100%)' }} />
            {laws && laws.length > 0 && (
              <div style={{ position: 'absolute', top: 16, insetInlineEnd: 16, display: 'flex', alignItems: 'center', gap: 8, background: 'rgba(255,255,255,.94)', borderRadius: 20, padding: '5px 12px 5px 10px', fontSize: 12, fontWeight: 600, boxShadow: '0 4px 14px rgba(0,0,0,.12)' }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#2E7D5B' }} />
                {laws.length} وثائق مفهرسة
              </div>
            )}
          </div>
          {/* Live sample: a real article from the indexed corpus, never hard-coded text. */}
          <div style={{ position: 'relative', width: 'min(100%,430px)' }}>
            <div style={{ background: '#fff', border: '1px solid #E4E0D6', borderRadius: 14, boxShadow: '0 30px 60px -20px rgba(27,31,29,.35),0 2px 6px rgba(27,31,29,.06)', overflow: 'hidden' }}>
              <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, padding: '12px 18px', borderBottom: '1px solid #EEEAE1' }}>
                <span style={{ whiteSpace: 'nowrap', background: '#E2EFE7', color: '#17543D', fontSize: 12, fontWeight: 600, padding: '2px 10px', borderRadius: 20 }}>من المدونة المعتمدة</span>
                <span style={{ whiteSpace: 'nowrap', fontSize: 12, color: '#5E655F' }}>
                  النص النافذ حتى <span dir="ltr" className="mono">{todayIso()}</span>
                </span>
              </div>
              {sample ? (
                <>
                  <p className="naskh" style={{ margin: 0, padding: '16px 18px', fontSize: 16, lineHeight: 2 }}>{sample.text}</p>
                  <div style={{ margin: '0 18px 16px', border: '1px solid #A9CDB9', background: '#F3F8F5', borderRadius: 8, padding: '10px 12px', display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span className="mono" style={{ fontSize: 12, fontWeight: 600, color: '#1F5F4A', background: '#E6EFEA', borderRadius: 4, padding: '1px 6px' }}>1</span>
                    <div style={{ flex: 1, lineHeight: 1.4 }}>
                      <div style={{ fontWeight: 600, fontSize: 13.5 }}>
                        {sample.law} — {sample.article}
                      </div>
                      <div style={{ fontSize: 12, color: '#5E655F' }}>سارية منذ {fmtDate(sample.effective)}</div>
                    </div>
                    <span style={{ whiteSpace: 'nowrap', background: '#E2EFE7', color: '#17543D', fontSize: 11, fontWeight: 600, padding: '1px 8px', borderRadius: 20 }}>سارية</span>
                  </div>
                </>
              ) : (
                <p style={{ margin: 0, padding: '16px 18px', fontSize: 14.5, lineHeight: 1.9, color: '#5E655F' }}>
                  {laws === null ? 'جارٍ التحميل…' : 'لا توجد وثائق معتمدة نافذة في المدونة بعد. تظهر هنا مادة من أول وثيقة يعتمدها المراجع.'}
                </p>
              )}
            </div>
          </div>
        </div>
      </section>

      <section id="how" style={{ borderTop: '1px solid #E4E0D6', background: '#fff' }}>
        <div style={{ ...WRAP, padding: '72px 28px', display: 'flex', flexDirection: 'column', gap: 36 }}>
          <h2 style={h2}>كيف يعمل</h2>
          <ol style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 32 }}>
            {STEPS.map((st) => (
              <li key={st.n} style={{ display: 'flex', flexDirection: 'column', gap: 10, borderTop: '2px solid #1F5F4A', paddingTop: 18 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ width: 44, height: 44, borderRadius: 12, background: '#E6EFEA', display: 'grid', placeItems: 'center' }}>
                    <Icon d={st.icon} size={22} stroke="#1F5F4A" />
                  </span>
                  <span style={{ flex: 1 }} />
                  <span dir="ltr" className="mono" style={{ fontSize: 13, color: '#1F5F4A', fontWeight: 500 }}>{st.n}</span>
                </div>
                <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>{st.title}</h3>
                <p style={{ margin: 0, color: '#3F4541', fontSize: 14.5, textWrap: 'pretty' }}>{st.body}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section id="trust" style={{ borderTop: '1px solid #E4E0D6' }}>
        <div style={{ ...WRAP, padding: '72px 28px', display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,320px),1fr))', gap: 48 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <h2 style={{ ...h2, lineHeight: 1.4 }}>الاستشهاد الخاطئ إجابة خاطئة.</h2>
            <p style={{ margin: 0, color: '#3F4541', textWrap: 'pretty' }}>لذلك بُني المساعد على قواعد ثابتة لا تتجاوزها أي إجابة.</p>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 1, background: '#E4E0D6', border: '1px solid #E4E0D6', borderRadius: 12, overflow: 'hidden' }}>
            {PRINCIPLES.map((pr) => (
              <div key={pr.title} style={{ background: '#fff', padding: '22px 22px 24px', display: 'flex', flexDirection: 'column', gap: 6 }}>
                <span style={{ marginBottom: 6 }}>
                  <Icon d={pr.icon} size={24} stroke="#1F5F4A" />
                </span>
                <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>{pr.title}</h3>
                <p style={{ margin: 0, color: '#3F4541', fontSize: 14, textWrap: 'pretty' }}>{pr.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="coverage" style={{ borderTop: '1px solid #E4E0D6', background: '#fff' }}>
        <div style={{ ...WRAP, padding: '72px 28px', display: 'flex', flexDirection: 'column', gap: 28 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: 16 }}>
            <h2 style={h2}>التغطية الحالية</h2>
            <span style={{ color: '#5E655F', fontSize: 14 }}>
              {laws === null ? 'جارٍ التحميل…' : laws.length === 0 ? 'لا توجد وثائق معتمدة بعد.' : `${laws.length} وثائق في ${domains.length} مجالات. ما لم يُفهرس لا يُجاب عنه.`}
            </span>
            {anyTest && <span style={{ fontSize: 11, fontWeight: 600, color: '#7A4F00', background: '#FBF0D9', borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap' }}>بيانات تجريبية وليست نصوصًا رسمية</span>}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(260px,1fr))', gap: 16 }}>
            {domains.map((domain) => {
              const docs = (laws ?? []).filter((l) => l.domain === domain)
              const sources = [...new Set(docs.map((l) => l.issuing_authority).filter(Boolean))].join(' · ')
              return (
                <div key={domain} style={{ border: '1px solid #E4E0D6', borderRadius: 12, padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: 12, background: '#F6F4EF' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <span style={{ width: 40, height: 40, borderRadius: 10, background: '#fff', border: '1px solid #E4E0D6', display: 'grid', placeItems: 'center' }}>
                      <Icon d={DOMAIN_ICON[domain] ?? ICON.doc} size={20} stroke="#1F5F4A" />
                    </span>
                    <h3 style={{ margin: 0, fontSize: 17, fontWeight: 700 }}>{DOMAIN_AR[domain] ?? domain}</h3>
                  </div>
                  <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {docs.map((l) => (
                      <li key={l.id} style={{ fontSize: 14, color: '#3F4541', display: 'flex', gap: 8 }}>
                        <span style={{ color: '#1F5F4A' }}>—</span>
                        {l.official_title_ar}
                      </li>
                    ))}
                  </ul>
                  {sources && <span style={{ fontSize: 12, color: '#5E655F', marginTop: 'auto' }}>المصدر: {sources}</span>}
                </div>
              )
            })}
          </div>
        </div>
      </section>

      <section style={{ borderTop: '1px solid #E4E0D6', background: '#1F5F4A', color: '#fff' }}>
        <div style={{ ...WRAP, padding: '56px 28px', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 24 }}>
          <h2 style={{ margin: 0, flex: 1, minWidth: 280, fontFamily: SERIF, fontSize: 28, fontWeight: 700, lineHeight: 1.5 }}>اسأل سؤالك الأول الآن.</h2>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            <button onClick={onTry} style={{ ...primary, background: '#fff', color: '#1F5F4A' }}>جرّب دون تسجيل</button>
            <button onClick={onSignup} style={{ whiteSpace: 'nowrap', border: '1px solid rgba(255,255,255,.5)', background: 'transparent', color: '#fff', borderRadius: 10, padding: '12px 22px', fontSize: 15, fontWeight: 600, cursor: 'pointer' }}>إنشاء حساب</button>
          </div>
        </div>
      </section>

      <footer style={{ borderTop: '1px solid #E4E0D6' }}>
        <div style={{ ...WRAP, padding: 28, fontSize: 12.5, color: '#5E655F' }}>المساعد يقدّم معلومات قانونية عامة من نصوص مفهرسة، ولا يقدّم استشارة قانونية ولا ينشئ علاقة محامٍ بموكل. لحالتك الخاصة استشر محاميًا مرخّصًا.</div>
      </footer>
    </div>
  )
}

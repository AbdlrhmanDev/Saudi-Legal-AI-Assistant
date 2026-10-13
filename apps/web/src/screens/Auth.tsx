// Login, sign-up, forgot-password and reset-password screens from "Saudi Legal AI Site.dc.html".
import { useEffect, useRef, useState } from 'react'
import type { CSSProperties, FormEvent, ReactNode } from 'react'
import { ApiError, api, setToken } from '../api'
import type { User } from '../api'
import { Icon, Logo } from '../ui'
import { ICON } from '../uiData'
import { Photo, SERIF } from './Site'

export type AuthView = 'login' | 'signup' | 'forgot' | 'reset'

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const strong = (pw: string) => pw.length >= 10 && /\d/.test(pw) && /[A-Za-z]/.test(pw)

const label: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13.5, fontWeight: 500 }
const fieldError: CSSProperties = { fontSize: 12.5, color: '#8E2626', fontWeight: 400 }
const submitStyle = (busy: boolean): CSSProperties => ({ whiteSpace: 'nowrap', border: 0, background: '#1F5F4A', color: '#fff', borderRadius: 10, padding: '12px 18px', fontSize: 15, fontWeight: 600, cursor: 'pointer', opacity: busy ? 0.6 : 1 })
const input = (bad: boolean): CSSProperties => ({ fontSize: 15, padding: '10px 12px', border: `1px solid ${bad ? '#B54242' : '#D9D4C8'}`, borderRadius: 8, background: '#fff', color: '#1B1F1D', textAlign: 'right' })
const column: CSSProperties = { margin: 'auto 0', padding: '48px 0', width: '100%', maxWidth: 400, alignSelf: 'center', display: 'flex', flexDirection: 'column', gap: 18 }
const linkButton: CSSProperties = { border: 0, background: 'transparent', padding: 0, color: '#1F5F4A', cursor: 'pointer', fontWeight: 600, fontSize: 'inherit' }

function Heading({ title, children, size = 30 }: { title: string; children: ReactNode; size?: number }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <h1 style={{ margin: 0, fontFamily: SERIF, fontSize: size, fontWeight: 700 }}>{title}</h1>
      <p style={{ margin: 0, color: '#5E655F', fontSize: 14.5, textWrap: 'pretty' }}>{children}</p>
    </div>
  )
}

function Badge({ d }: { d: string }) {
  return (
    <span style={{ width: 52, height: 52, borderRadius: 14, background: '#E6EFEA', display: 'grid', placeItems: 'center' }}>
      <Icon d={d} size={24} stroke="#1F5F4A" />
    </span>
  )
}

function PasswordField({ value, onChange, show, onToggle, error, autoComplete, text }: { value: string; onChange: (v: string) => void; show: boolean; onToggle: () => void; error?: string; autoComplete: string; text: ReactNode }) {
  return (
    <label style={label}>
      {text}
      <span style={{ position: 'relative', display: 'flex' }}>
        <input name="password" type={show ? 'text' : 'password'} dir="ltr" autoComplete={autoComplete} value={value} onChange={(e) => onChange(e.target.value)} aria-invalid={!!error} style={{ ...input(!!error), flex: 1, paddingLeft: 64 }} />
        <button type="button" onClick={onToggle} style={{ whiteSpace: 'nowrap', position: 'absolute', left: 6, top: '50%', transform: 'translateY(-50%)', border: 0, background: 'transparent', color: '#1F5F4A', fontSize: 12.5, fontWeight: 600, cursor: 'pointer', padding: '4px 8px' }}>
          {show ? 'إخفاء' : 'إظهار'}
        </button>
      </span>
      {error && <span style={fieldError}>{error}</span>}
    </label>
  )
}

function Rules({ password }: { password: string }) {
  const rules: [string, boolean][] = [
    ['10 أحرف على الأقل', password.length >= 10],
    ['حرف لاتيني', /[A-Za-z]/.test(password)],
    ['رقم', /\d/.test(password)],
  ]
  return (
    <ul aria-label="شروط كلمة المرور" style={{ margin: '-8px 0 0', padding: 0, listStyle: 'none', display: 'flex', flexWrap: 'wrap', gap: '6px 16px' }}>
      {rules.map(([text, ok]) => (
        <li key={text} style={{ fontSize: 12.5, color: ok ? '#17543D' : '#5E655F', display: 'flex', gap: 6, whiteSpace: 'nowrap' }}>
          <span>{ok ? '✓' : '○'}</span>
          {text}
        </li>
      ))}
    </ul>
  )
}

const failure = (err: unknown, invalid: string) => {
  if (!(err instanceof ApiError)) return 'تعذّر إكمال الطلب.'
  if (err.status === 429) return 'محاولات كثيرة. حاول بعد قليل.'
  if (err.status === 0) return 'تعذّر الاتصال بالخادم.'
  return invalid
}

type Props = { view: AuthView; resetToken: string | null; go: (view: AuthView | 'landing' | 'app', resetToken?: string) => void; onSession: (user: User) => void }

export function AuthPage({ view, resetToken, go, onSession }: Props) {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [terms, setTerms] = useState(false)
  const [remember, setRemember] = useState(true)
  const [showPw, setShowPw] = useState(false)
  const [touched, setTouched] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [devToken, setDevToken] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [cooldown, setCooldown] = useState(0)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearInterval(timer.current), [])

  const startCooldown = () => {
    window.clearInterval(timer.current)
    setCooldown(30)
    timer.current = window.setInterval(() => setCooldown((c) => (c <= 1 ? (window.clearInterval(timer.current), 0) : c - 1)), 1000)
  }

  const errors: Record<string, string> = {}
  if (touched) {
    if (view === 'signup' && name.trim().length < 2) errors.name = 'أدخل اسمك.'
    if (view !== 'reset' && !EMAIL.test(email.trim())) errors.email = 'أدخل بريدًا إلكترونيًا صحيحًا.'
    if (view === 'login' && !password) errors.password = 'أدخل كلمة المرور.'
    if ((view === 'signup' || view === 'reset') && !strong(password)) errors.password = 'كلمة المرور لا تستوفي الشروط.'
    if (view === 'reset' && confirm !== password) errors.confirm = 'كلمتا المرور غير متطابقتين.'
    if (view === 'signup' && !terms) errors.terms = 'يلزم الإقرار للمتابعة.'
  }
  const valid =
    (view === 'reset' || EMAIL.test(email.trim())) &&
    (view !== 'login' || !!password) &&
    (view !== 'signup' || (name.trim().length >= 2 && strong(password) && terms)) &&
    (view !== 'reset' || (strong(password) && confirm === password))

  const requestReset = async () => {
    const data = await api<{ dev_reset_token?: string }>('/api/v1/auth/forgot', { method: 'POST', body: { email: email.trim() } })
    setDevToken(data.dev_reset_token ?? null)
    setSent(true)
    startCooldown()
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    setError('')
    if (!valid) return
    setBusy(true)
    try {
      if (view === 'login' || view === 'signup') {
        const body = view === 'login' ? { email: email.trim(), password } : { name: name.trim(), email: email.trim(), password, accept_terms: true }
        const data = await api<{ access_token: string; user: User }>(`/api/v1/auth/${view === 'login' ? 'login' : 'register'}`, { method: 'POST', body })
        setToken(data.access_token, view === 'login' ? remember : false)
        onSession(data.user)
      } else if (view === 'forgot') {
        await requestReset()
      } else {
        await api('/api/v1/auth/reset', { method: 'POST', body: { token: resetToken ?? '', password } })
        setDone(true)
      }
    } catch (err) {
      const code = err instanceof ApiError ? err.code : ''
      setError(
        code === 'EMAIL_TAKEN'
          ? 'يوجد حساب بهذا البريد. سجّل الدخول أو استعد كلمة المرور.'
          : failure(err, view === 'login' ? 'البريد أو كلمة المرور غير صحيحة.' : view === 'reset' ? 'رابط إعادة التعيين غير صالح أو انتهت صلاحيته. اطلب رابطًا جديدًا.' : 'تعذّر إكمال الطلب. تحقق من البيانات.'),
      )
    } finally {
      setBusy(false)
    }
  }

  const alert = error && (
    <div role="alert" style={{ background: '#F8E3E1', color: '#8E2626', borderRadius: 8, padding: '10px 14px', fontSize: 13.5 }}>
      {error}
    </div>
  )
  const backToLogin = (
    <button type="button" onClick={() => go('login')} style={{ ...linkButton, alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13.5, fontWeight: 500 }}>
      <Icon d="M5 12h14M12 5l7 7-7 7" size={16} sw={2} />
      العودة لتسجيل الدخول
    </button>
  )
  const emailField = (
    <label style={label}>
      البريد الإلكتروني
      <input name="email" type="email" dir="ltr" autoComplete="email" placeholder="name@example.com" value={email} onChange={(e) => setEmail(e.target.value)} aria-invalid={!!errors.email} style={input(!!errors.email)} />
      {errors.email && <span style={fieldError}>{errors.email}</span>}
    </label>
  )

  let body: ReactNode
  if (view === 'forgot' && !sent) {
    body = (
      <form onSubmit={submit} noValidate style={column}>
        {backToLogin}
        <Badge d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
        <Heading title="نسيت كلمة المرور؟">أدخل بريدك المسجّل لإصدار رابط تعيين كلمة مرور جديدة. صلاحية الرابط 30 دقيقة.</Heading>
        {alert}
        {emailField}
        <button type="submit" disabled={busy} style={submitStyle(busy)}>
          {busy ? 'جارٍ الإرسال…' : 'إرسال رابط إعادة التعيين'}
        </button>
      </form>
    )
  } else if (view === 'forgot') {
    body = (
      <div role="status" style={column}>
        {backToLogin}
        <Badge d="M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM22 6l-10 7L2 6" />
        <Heading title="تحقّق من بريدك">
          إذا كان <span dir="ltr" style={{ fontWeight: 600, color: '#1B1F1D' }}>{email.trim()}</span> مسجّلًا لدينا، فقد صدر له رابط إعادة التعيين.
        </Heading>
        <ul style={{ margin: 0, padding: '12px 16px', listStyle: 'none', background: '#fff', border: '1px solid #E4E0D6', borderRadius: 10, display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13.5, color: '#3F4541' }}>
          <li>• الرابط يعمل مرة واحدة وينتهي بعد 30 دقيقة.</li>
          <li>• {devToken ? 'بيئة تجريبية: لا تُرسل رسائل بريد، والرابط متاح أدناه مباشرة.' : 'إرسال البريد يتولاه مشغّل الخدمة. تحقّق من مجلد الرسائل غير المرغوب فيها.'}</li>
        </ul>
        {alert}
        {devToken && (
          <button type="button" onClick={() => go('reset', devToken)} style={submitStyle(false)}>
            فتح رابط إعادة التعيين (تجريبي)
          </button>
        )}
        <p style={{ margin: 0, textAlign: 'center', fontSize: 14, color: '#5E655F' }}>
          لم يصلك الرابط؟{' '}
          <button type="button" disabled={cooldown > 0} onClick={() => requestReset().catch((err) => setError(failure(err, 'تعذّر إكمال الطلب.')))} style={{ ...linkButton, fontSize: 14, color: cooldown > 0 ? '#5E655F' : '#1F5F4A' }}>
            {cooldown > 0 ? `إعادة الإرسال بعد ${cooldown} ث` : 'إعادة الإرسال'}
          </button>
        </p>
        <button type="button" onClick={() => setSent(false)} style={{ ...linkButton, textAlign: 'center', fontSize: 13.5, fontWeight: 400 }}>
          استخدام بريد آخر
        </button>
      </div>
    )
  } else if (view === 'reset' && !done) {
    body = (
      <form onSubmit={submit} noValidate style={column}>
        <Badge d={ICON.lock} />
        <Heading title="تعيين كلمة مرور جديدة">اختر كلمة مرور قوية لهذا الحساب.</Heading>
        {!resetToken && (
          <div role="alert" style={{ background: '#F8E3E1', color: '#8E2626', borderRadius: 8, padding: '10px 14px', fontSize: 13.5 }}>
            لا يوجد رابط إعادة تعيين صالح.{' '}
            <button type="button" onClick={() => go('forgot')} style={{ ...linkButton, color: '#8E2626', textDecoration: 'underline' }}>
              اطلب رابطًا جديدًا
            </button>
          </div>
        )}
        {alert}
        <PasswordField text="كلمة المرور الجديدة" value={password} onChange={setPassword} show={showPw} onToggle={() => setShowPw(!showPw)} error={errors.password} autoComplete="new-password" />
        <Rules password={password} />
        <label style={label}>
          تأكيد كلمة المرور
          <input name="confirm" type={showPw ? 'text' : 'password'} dir="ltr" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} aria-invalid={!!errors.confirm} style={input(!!errors.confirm)} />
          {errors.confirm && <span style={fieldError}>{errors.confirm}</span>}
        </label>
        <button type="submit" disabled={busy || !resetToken} style={submitStyle(busy || !resetToken)}>
          {busy ? 'جارٍ الحفظ…' : 'حفظ كلمة المرور'}
        </button>
      </form>
    )
  } else if (view === 'reset') {
    body = (
      <div role="status" style={column}>
        <Badge d="M22 11.08V12a10 10 0 1 1-5.93-9.14M22 4 12 14.01l-3-3" />
        <Heading title="تم تغيير كلمة المرور">سجّل الدخول بكلمة المرور الجديدة. أُنهيت الجلسات المفتوحة سابقًا احتياطًا.</Heading>
        <button type="button" onClick={() => go('login')} style={submitStyle(false)}>
          تسجيل الدخول
        </button>
      </div>
    )
  } else {
    const isLogin = view === 'login'
    body = (
      <form onSubmit={submit} noValidate style={column}>
        <Heading title={isLogin ? 'تسجيل الدخول' : 'إنشاء حساب'} size={32}>
          {isLogin ? 'أهلًا بعودتك. المراجعون يصلون من هنا إلى المدونة والتحديثات.' : 'حساب للقراءة وطرح الأسئلة. يمكنك أيضًا السؤال دون حساب.'}
        </Heading>
        {alert}
        {!isLogin && (
          <label style={label}>
            الاسم
            <input name="name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!errors.name} style={{ ...input(!!errors.name), textAlign: 'start' }} />
            {errors.name && <span style={fieldError}>{errors.name}</span>}
          </label>
        )}
        {emailField}
        <PasswordField
          text={
            <span style={{ display: 'flex', alignItems: 'baseline' }}>
              <span style={{ flex: 1 }}>كلمة المرور</span>
              {isLogin && (
                <button type="button" onClick={() => go('forgot')} style={{ ...linkButton, fontSize: 12.5, fontWeight: 500 }}>
                  نسيت كلمة المرور؟
                </button>
              )}
            </span>
          }
          value={password}
          onChange={setPassword}
          show={showPw}
          onToggle={() => setShowPw(!showPw)}
          error={errors.password}
          autoComplete={isLogin ? 'current-password' : 'new-password'}
        />
        {!isLogin && (
          <>
            <Rules password={password} />
            <label style={{ display: 'flex', gap: 10, alignItems: 'flex-start', fontSize: 13.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={terms} onChange={(e) => setTerms(e.target.checked)} style={{ marginTop: 5, accentColor: '#1F5F4A' }} />
              <span style={{ color: '#3F4541' }}>أفهم أن المساعد يقدّم معلومات عامة وليست استشارة قانونية، وأوافق على حفظ بريدي واسمي لإدارة الحساب.</span>
            </label>
            {errors.terms && <span style={{ ...fieldError, marginTop: -10 }}>{errors.terms}</span>}
          </>
        )}
        {isLogin && (
          <label style={{ display: 'flex', gap: 10, alignItems: 'center', fontSize: 13.5, cursor: 'pointer', color: '#3F4541' }}>
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} style={{ accentColor: '#1F5F4A' }} />
            تذكّرني على هذا الجهاز
          </label>
        )}
        <button type="submit" disabled={busy} style={submitStyle(busy)}>
          {busy ? 'جارٍ التحقق…' : isLogin ? 'تسجيل الدخول' : 'إنشاء الحساب'}
        </button>
        <p style={{ margin: 0, textAlign: 'center', fontSize: 14, color: '#5E655F' }}>
          {isLogin ? 'ليس لديك حساب؟' : 'لديك حساب؟'}{' '}
          <button type="button" onClick={() => go(isLogin ? 'signup' : 'login')} style={{ ...linkButton, fontSize: 14 }}>
            {isLogin ? 'أنشئ حسابًا' : 'سجّل الدخول'}
          </button>
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, color: '#8C918C', fontSize: 12.5 }}>
          <span style={{ flex: 1, height: 1, background: '#E4E0D6' }} />
          أو
          <span style={{ flex: 1, height: 1, background: '#E4E0D6' }} />
        </div>
        <button type="button" onClick={() => go('app')} style={{ textAlign: 'center', border: '1px solid #D9D4C8', background: '#fff', borderRadius: 10, padding: '11px 18px', fontSize: 14.5, fontWeight: 600, color: '#1B1F1D', cursor: 'pointer' }}>
          المتابعة دون حساب
        </button>
      </form>
    )
  }

  const quote =
    view === 'forgot' || view === 'reset'
      ? 'لا نكشف إن كان البريد مسجّلًا لدينا، ويصدر رابط الاستعادة لصاحبه فقط.'
      : view === 'login'
        ? '«كل إجابة تبدأ من نص المادة، وتنتهي برابط مصدرها.»'
        : 'اسأل بالعربية، واحصل على المادة والإصدار وتاريخ النفاذ.'

  return (
    <div dir="rtl" lang="ar" style={{ fontFamily: "'IBM Plex Sans Arabic',sans-serif", color: '#1B1F1D', background: '#F6F4EF', minHeight: '100vh', fontSize: 15, lineHeight: 1.7, display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,420px),1fr))' }}>
      <div style={{ display: 'flex', flexDirection: 'column', padding: '28px clamp(24px,5vw,72px)' }}>
        <button onClick={() => go('landing')} style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center', gap: 10, border: 0, background: 'transparent', cursor: 'pointer', padding: 0, color: '#1B1F1D' }}>
          <Logo size={32} />
          <span style={{ fontWeight: 700, fontSize: 16, whiteSpace: 'nowrap' }}>المساعد القانوني</span>
        </button>
        {body}
        <span style={{ fontSize: 12, color: '#5E655F' }}>حسابات المراجعين يُنشئها مسؤول النظام.</span>
      </div>
      <aside style={{ background: '#1F5F4A', color: '#fff', padding: 'clamp(32px,5vw,72px)', display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: 28 }}>
        <div style={{ width: '100%', maxWidth: 520, aspectRatio: '16/10', borderRadius: 16, overflow: 'hidden', background: '#2A6B55' }}>
          <Photo src="/assets/riyadh-night.jpg" alt="الرياض ليلًا" />
        </div>
        <p style={{ margin: 0, fontFamily: SERIF, fontSize: 'clamp(24px,2.6vw,34px)', lineHeight: 1.7, fontWeight: 600, maxWidth: 520, textWrap: 'pretty' }}>{quote}</p>
        <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 440 }}>
          {['استشهاد بالمادة والإصدار في كل إجابة', 'تمييز النص الساري عن المستقبلي والمُلغى', 'امتناع صريح عند غياب المصدر'].map((point) => (
            <li key={point} style={{ display: 'flex', gap: 12, alignItems: 'center', fontSize: 14.5, color: '#DCEDE3' }}>
              <Icon d={ICON.check} stroke="#fff" />
              {point}
            </li>
          ))}
        </ul>
      </aside>
    </div>
  )
}

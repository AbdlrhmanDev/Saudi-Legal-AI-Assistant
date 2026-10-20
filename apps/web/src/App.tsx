import { useCallback, useEffect, useRef, useState } from 'react'
import { api, hasToken, setToken } from './api'
import type { Candidate, Coverage, Freshness, Page, QuarantineItem, User } from './api'
import { Chat } from './screens/Chat'
import { Corpus } from './screens/Corpus'
import { Ops } from './screens/Ops'
import { Updates } from './screens/Updates'
import { Icon, Logo } from './ui'
import { DOMAIN_AR, ICON, fmtRiyadh } from './uiData'
import type { Flash } from './uiData'
import { useChat } from './useChat'

type Screen = 'chat' | 'corpus' | 'updates' | 'ops'

const NAV: { key: Screen; label: string; icon: string }[] = [
  { key: 'corpus', label: 'المدونة النظامية', icon: ICON.book },
  { key: 'updates', label: 'التحديثات', icon: ICON.refresh },
  { key: 'ops', label: 'العمليات والتقييم', icon: ICON.pulse },
]
const HEALTH_DOT = { healthy: '#2E7D5B', delayed: '#C08A1E', degraded: '#9B2C2C' } as const

export default function App({ onLogin, onLogout }: { onLogin: () => void; onLogout: () => void }) {
  const [user, setUser] = useState<User | null>(null)
  const [screen, setScreen] = useState<Screen>('chat')
  const [toast, setToast] = useState<{ text: string; bg: string } | null>(null)
  const [coverage, setCoverage] = useState<Coverage | null>(null)
  const [freshness, setFreshness] = useState<Freshness | null>(null)
  const [pendingCount, setPendingCount] = useState(0)
  const toastTimer = useRef<number | undefined>(undefined)
  const chat = useChat()

  const flash = useCallback<Flash>((text, kind) => {
    window.clearTimeout(toastTimer.current)
    setToast({ text, bg: kind === 'err' ? '#8E2626' : '#1B1F1D' })
    toastTimer.current = window.setTimeout(() => setToast(null), 3800)
  }, [])

  const isAdmin = user?.role === 'admin'
  const active: Screen = isAdmin ? screen : 'chat' // admin screens never render for visitors

  const refreshShell = useCallback(() => {
    api<Coverage>('/api/v1/coverage').then(setCoverage).catch(() => setCoverage(null))
    api<Freshness>('/api/v1/freshness').then(setFreshness).catch(() => setFreshness(null))
    if (hasToken()) {
      // Routine updates publish themselves; the badge counts only what still needs a person.
      Promise.all([
        api<Page<Candidate>>('/api/v1/admin/updates?state=needs_review&limit=1'),
        api<Page<QuarantineItem>>('/api/v1/admin/quarantine?limit=1'),
      ])
        .then(([pending, quarantined]) => setPendingCount(pending.pagination.total + quarantined.pagination.total))
        .catch(() => setPendingCount(0))
    }
  }, [])

  useEffect(() => {
    if (hasToken()) api<User>('/api/v1/auth/me').then(setUser).catch(() => setUser(null))
    const expired = () => {
      setUser(null)
      setScreen('chat')
      flash('انتهت الجلسة. سجّل الدخول من جديد.', 'err')
    }
    window.addEventListener('auth:expired', expired)
    return () => window.removeEventListener('auth:expired', expired)
  }, [flash])

  useEffect(refreshShell, [refreshShell, user])

  const logout = () => {
    setToken(null)
    setUser(null)
    setScreen('chat')
    setPendingCount(0)
    onLogout()
  }

  const navButton = (key: Screen, label: string, icon: string, badge?: number) => {
    const on = active === key
    return (
      <button
        key={key}
        onClick={() => setScreen(key)}
        aria-current={on ? 'page' : undefined}
        style={{ whiteSpace: 'nowrap', display: 'flex', alignItems: 'center', gap: 12, padding: '9px 12px', border: 0, borderRadius: 10, background: on ? '#fff' : 'transparent', color: on ? '#1B1F1D' : '#3F4541', boxShadow: on ? '0 1px 2px rgba(27,31,29,.08)' : 'none', fontSize: 14, fontWeight: on ? 600 : 500, cursor: 'pointer', textAlign: 'start' }}
      >
        <span style={{ color: on ? '#1F5F4A' : '#5E655F', display: 'flex' }}>
          <Icon d={icon} />
        </span>
        <span style={{ flex: 1 }}>{label}</span>
        {!!badge && (
          <span style={{ background: '#9B2C2C', color: '#fff', fontSize: 11, fontWeight: 600, borderRadius: 10, minWidth: 20, textAlign: 'center', padding: '0 6px', lineHeight: '20px' }}>{badge}</span>
        )}
      </button>
    )
  }

  const documents = coverage?.domains.reduce((n, d) => n + d.instruments, 0) ?? 0
  const domainNames = coverage?.domains.map((d) => DOMAIN_AR[d.domain] ?? d.domain).join('، ')

  return (
    <div dir="rtl" lang="ar" className="shell">
      <aside className="sidebar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0 6px' }}>
          <Logo />
          <div className="brand-text" style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.3 }}>
            <span style={{ fontWeight: 700, fontSize: 15 }}>المساعد القانوني</span>
            <span style={{ fontSize: 12, color: '#5E655F' }}>الأنظمة السعودية</span>
          </div>
        </div>

        <nav aria-label="التنقل الرئيسي" className="nav">
          {navButton('chat', 'اسأل عن نظام', ICON.chat)}
          {isAdmin && (
            <>
              <span className="nav-heading" style={{ fontSize: 11.5, fontWeight: 600, color: '#5E655F', padding: '18px 12px 6px' }}>الإدارة والمراجعة</span>
              {NAV.map((n) => navButton(n.key, n.label, n.icon, n.key === 'updates' ? pendingCount : undefined))}
            </>
          )}
        </nav>

        <div className="sidebar-foot">
          <div className="coverage-card" style={{ background: '#F7F5F0', border: '1px solid #E1DCD0', borderRadius: 12, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, fontWeight: 600 }}>
              <span style={{ color: '#1F5F4A', display: 'flex' }}>
                <Icon d={ICON.doc} size={16} />
              </span>
              التغطية الحالية
            </div>
            <div style={{ fontSize: 12, color: '#5E655F', lineHeight: 1.7 }}>
              {coverage === null ? 'تعذّر تحميل التغطية.' : documents === 0 ? 'لا توجد وثائق معتمدة نافذة بعد.' : `${documents} وثائق معتمدة: ${domainNames}.`}
            </div>
            {freshness && (
              <div style={{ fontSize: 11.5, color: '#5E655F', lineHeight: 1.6, display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', flex: 'none', background: HEALTH_DOT[freshness.source_health] }} />
                آخر مزامنة موثّقة: {freshness.corpus_verified_at ? fmtRiyadh(freshness.corpus_verified_at) : 'لم تتم بعد'}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 6px 0', borderTop: '1px solid #E1DCD0' }}>
            <span style={{ width: 34, height: 34, borderRadius: '50%', background: '#DCE7E0', color: '#17543D', display: 'grid', placeItems: 'center', fontSize: 13, fontWeight: 700, flex: 'none' }}>
              {user ? (user.name ?? user.email).slice(0, 1).toUpperCase() : 'ز'}
            </span>
            <div className="user-text" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', lineHeight: 1.3 }}>
              <span dir="auto" style={{ fontSize: 13.5, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{user ? (user.name ?? user.email) : 'زائر'}</span>
              <span style={{ fontSize: 11.5, color: '#5E655F' }}>{isAdmin ? 'مراجِع' : user ? 'حساب قراءة' : 'وصول للقراءة'}</span>
            </div>
            <button
              onClick={user ? logout : onLogin}
              aria-label={user ? 'تسجيل الخروج' : 'تسجيل الدخول'}
              title={user ? 'تسجيل الخروج' : 'تسجيل الدخول'}
              style={{ width: 32, height: 32, borderRadius: 8, display: 'grid', placeItems: 'center', color: '#5E655F', border: 0, background: 'transparent', cursor: 'pointer', flex: 'none' }}
            >
              <Icon d={user ? ICON.logout : ICON.login} size={17} />
            </button>
          </div>
        </div>
      </aside>

      <main style={{ minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {active === 'chat' && <Chat chat={chat} freshness={freshness} />}
        {active === 'corpus' && <Corpus flash={flash} onChanged={refreshShell} onOpenReview={() => setScreen('updates')} />}
        {active === 'updates' && <Updates flash={flash} onChanged={refreshShell} asOf={chat.asOf} />}
        {active === 'ops' && <Ops />}
      </main>

      {toast && (
        <div role="status" style={{ position: 'fixed', bottom: 24, left: '50%', transform: 'translateX(-50%)', background: toast.bg, color: '#fff', padding: '10px 18px', borderRadius: 8, fontSize: 13.5, boxShadow: '0 6px 20px rgba(0,0,0,.18)', maxWidth: 560, zIndex: 30 }}>
          {toast.text}
        </div>
      )}
    </div>
  )
}

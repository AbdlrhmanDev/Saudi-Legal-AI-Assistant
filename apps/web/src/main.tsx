import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'
import { AuthPage } from './screens/Auth'
import type { AuthView } from './screens/Auth'
import { Landing } from './screens/Site'

type View = 'landing' | 'app' | AuthView
const AUTH: AuthView[] = ['login', 'signup', 'forgot', 'reset']

// Hash routes: "" = home page, "#app", "#login", "#signup", "#forgot", "#reset/<token>".
// Section links (#how, #trust, #coverage) stay on the home page.
function parse(hash: string): { view: View; resetToken: string | null } {
  const [name, token] = hash.replace(/^#/, '').split('/')
  if (name === 'app') return { view: 'app', resetToken: null }
  if (AUTH.includes(name as AuthView)) return { view: name as AuthView, resetToken: name === 'reset' ? (token ?? null) : null }
  return { view: 'landing', resetToken: null }
}

export function Root() {
  const [route, setRoute] = useState(() => parse(location.hash))
  useEffect(() => {
    const onHash = () => setRoute(parse(location.hash))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const go = (next: View, resetToken?: string) => {
    location.hash = next === 'landing' ? '' : next === 'reset' && resetToken ? `reset/${resetToken}` : next
    window.scrollTo(0, 0)
  }

  if (route.view === 'app') return <App onLogin={() => go('login')} onLogout={() => go('login')} />
  if (route.view === 'landing') return <Landing onTry={() => go('app')} onLogin={() => go('login')} onSignup={() => go('signup')} />
  return <AuthPage key={`${route.view}:${route.resetToken ?? ''}`} view={route.view} resetToken={route.resetToken} go={go} onSession={() => go('app')} />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)

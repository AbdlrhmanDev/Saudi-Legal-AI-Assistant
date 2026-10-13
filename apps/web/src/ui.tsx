import type { CSSProperties, ReactNode } from 'react'
import { ICON, pillOf } from './uiData'

export function Pill({ k, size = 12, dot = true, style }: { k: string; size?: number; dot?: boolean; style?: CSSProperties }) {
  const p = pillOf(k)
  return (
    <span
      style={{ whiteSpace: 'nowrap', display: 'inline-flex', alignItems: 'center', gap: 6, background: p.bg, color: p.fg, fontSize: size, fontWeight: 600, padding: '2px 10px', borderRadius: 20, flex: 'none', ...style }}
    >
      {dot && <span style={{ width: 6, height: 6, borderRadius: '50%', background: p.fg }} />}
      {p.label}
    </span>
  )
}

export function Icon({ d, size = 18, sw = 1.8, stroke = 'currentColor' }: { d: string; size?: number; sw?: number; stroke?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: 'none', display: 'block' }}>
      <path d={d} />
    </svg>
  )
}

export function Logo({ size = 36 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" role="img" aria-label="شعار المساعد القانوني" style={{ flex: 'none', display: 'block' }}>
      <rect width="32" height="32" rx="8" fill="#1F5F4A" />
      <g fill="none" stroke="#fff" strokeWidth="1.6" strokeLinejoin="round">
        <rect x="9.5" y="9.5" width="13" height="13" />
        <rect x="9.5" y="9.5" width="13" height="13" transform="rotate(45 16 16)" />
      </g>
      <circle cx="16" cy="16" r="2.6" fill="#D9B45A" />
    </svg>
  )
}

export function TestBadge({ dashed }: { dashed?: boolean }) {
  return dashed ? (
    <span style={{ whiteSpace: 'nowrap', fontSize: 11, fontWeight: 600, color: '#7A4F00', border: '1px dashed #E2B65A', padding: '0 6px', borderRadius: 5 }}>تجريبي</span>
  ) : (
    <span style={{ alignSelf: 'flex-start', fontSize: 11, fontWeight: 600, color: '#7A4F00', background: '#FBF0D9', borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap' }}>بيانات تجريبية</span>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: '#FAF9F6', border: '1px dashed #D9D4C8', borderRadius: 14, padding: '16px 18px', fontSize: 13.5, color: '#5E655F' }}>
      <Icon d={ICON.info} size={17} />
      <span>{children}</span>
    </div>
  )
}


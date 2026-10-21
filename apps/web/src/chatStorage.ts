import type { ChatAnswer } from './api'

export type ChatMessage =
  | { role: 'user'; text: string }
  | { role: 'pending'; asOf: string }
  | { role: 'streaming'; text: string; asOf: string }
  | { role: 'assistant'; answer: ChatAnswer; latencyMs: number }
  | { role: 'error'; text: string }

const LEGACY_STORAGE_KEY = 'saudi-legal-ai:chat:v1'
const STORAGE_PREFIX = 'saudi-legal-ai:chat:v2:'
const GUEST_SESSION_KEY = 'saudi-legal-ai:guest-session:v1'
const MAX_MESSAGES = 50

type StoredChat = { version: 1; messages: ChatMessage[] }

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

function isSavedMessage(value: unknown): value is ChatMessage {
  if (!isObject(value) || typeof value.role !== 'string' || value.role === 'pending' || value.role === 'streaming') return false
  if (value.role === 'user' || value.role === 'error') return typeof value.text === 'string'
  if (value.role !== 'assistant' || !isObject(value.answer)) return false
  const answer = value.answer
  return (
    typeof value.latencyMs === 'number' &&
    typeof answer.answer_ar === 'string' &&
    typeof answer.answer_status === 'string' &&
    typeof answer.as_of_date === 'string' &&
    Array.isArray(answer.citations) &&
    Array.isArray(answer.caveats)
  )
}

const storageKey = (owner: string) => `${STORAGE_PREFIX}${encodeURIComponent(owner)}`

/** One guest tab is one private conversation owner; signing in uses the database user id instead. */
export function getGuestChatOwner(): string {
  try {
    const existing = sessionStorage.getItem(GUEST_SESSION_KEY)
    if (existing) return `guest:${existing}`
    const id = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`
    sessionStorage.setItem(GUEST_SESSION_KEY, id)
    return `guest:${id}`
  } catch {
    return `guest:ephemeral-${Date.now()}-${Math.random().toString(36).slice(2)}`
  }
}

export function loadChatMessages(owner: string): ChatMessage[] {
  try {
    // Version 1 used one shared key for every account. Never display that mixed history again.
    localStorage.removeItem(LEGACY_STORAGE_KEY)
    const raw = localStorage.getItem(storageKey(owner))
    if (!raw) return []
    const stored: unknown = JSON.parse(raw)
    if (!isObject(stored) || stored.version !== 1 || !Array.isArray(stored.messages)) return []
    return stored.messages.filter(isSavedMessage).slice(-MAX_MESSAGES)
  } catch {
    return []
  }
}

export function saveChatMessages(owner: string, messages: ChatMessage[]) {
  try {
    const saved = messages.filter((message) => message.role !== 'pending' && message.role !== 'streaming').slice(-MAX_MESSAGES)
    const payload: StoredChat = { version: 1, messages: saved }
    localStorage.setItem(storageKey(owner), JSON.stringify(payload))
  } catch {
    // Storage may be unavailable or full. Chat remains usable for the current tab.
  }
}

export function clearSavedChat(owner: string) {
  try {
    localStorage.removeItem(storageKey(owner))
  } catch {
    // Nothing else is required when browser storage is unavailable.
  }
}

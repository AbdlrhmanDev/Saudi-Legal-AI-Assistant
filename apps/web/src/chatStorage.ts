import type { ChatAnswer } from './api'

export type ChatMessage =
  | { role: 'user'; text: string }
  | { role: 'pending'; asOf: string }
  | { role: 'assistant'; answer: ChatAnswer; latencyMs: number }
  | { role: 'error'; text: string }

const STORAGE_KEY = 'saudi-legal-ai:chat:v1'
const MAX_MESSAGES = 50

type StoredChat = { version: 1; messages: ChatMessage[] }

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

function isSavedMessage(value: unknown): value is ChatMessage {
  if (!isObject(value) || typeof value.role !== 'string' || value.role === 'pending') return false
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

export function loadChatMessages(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const stored: unknown = JSON.parse(raw)
    if (!isObject(stored) || stored.version !== 1 || !Array.isArray(stored.messages)) return []
    return stored.messages.filter(isSavedMessage).slice(-MAX_MESSAGES)
  } catch {
    return []
  }
}

export function saveChatMessages(messages: ChatMessage[]) {
  try {
    const saved = messages.filter((message) => message.role !== 'pending').slice(-MAX_MESSAGES)
    const payload: StoredChat = { version: 1, messages: saved }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    // Storage may be unavailable or full. Chat remains usable for the current tab.
  }
}

export function clearSavedChat() {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing else is required when browser storage is unavailable.
  }
}

import { useCallback, useEffect, useState } from 'react'

import { api } from './api'
import type { ChatAnswer, Citation } from './api'
import { clearSavedChat, loadChatMessages, saveChatMessages } from './chatStorage'
import type { ChatMessage } from './chatStorage'
import { todayIso } from './uiData'

/** Conversation state lives above the screen so it survives navigation. */
export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>(loadChatMessages)
  const [domain, setDomain] = useState('all')
  const [asOf, setAsOf] = useState(todayIso)
  const [input, setInput] = useState('')
  const [source, setSource] = useState<Citation | null>(null)
  const busy = messages.some((message) => message.role === 'pending')

  const send = useCallback(
    async (textArg?: string) => {
      const text = (textArg ?? input).trim()
      if (!text || busy) return

      setInput('')
      setMessages((current) => [...current, { role: 'user', text }, { role: 'pending', asOf }])
      const started = performance.now()
      let reply: ChatMessage

      try {
        const answer = await api<ChatAnswer>('/api/v1/chat', {
          method: 'POST',
          body: { query: text, as_of_date: asOf, ...(domain !== 'all' ? { domain } : {}) },
        })
        reply = { role: 'assistant', answer, latencyMs: performance.now() - started }
        if (answer.citations[0] && window.innerWidth >= 1200) setSource(answer.citations[0])
      } catch (error) {
        reply = { role: 'error', text: error instanceof Error ? error.message : 'تعذّر إكمال الطلب.' }
      }

      setMessages((current) => [...current.filter((message) => message.role !== 'pending'), reply])
    },
    [asOf, busy, domain, input],
  )

  useEffect(() => saveChatMessages(messages), [messages])

  const clear = useCallback(() => {
    clearSavedChat()
    setMessages([])
    setSource(null)
  }, [])

  return { messages, domain, setDomain, asOf, setAsOf, input, setInput, source, setSource, send, clear, busy }
}

export type ChatController = ReturnType<typeof useChat>

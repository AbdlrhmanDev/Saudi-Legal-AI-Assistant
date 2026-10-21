import { useCallback, useEffect, useState } from 'react'

import { streamChat } from './api'
import type { ChatAnswer, Citation } from './api'
import { clearSavedChat, loadChatMessages, saveChatMessages } from './chatStorage'
import type { ChatMessage } from './chatStorage'
import { todayIso } from './uiData'

/** Conversation state lives above the screen so it survives navigation. */
export function useChat(owner: string | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loadedOwner, setLoadedOwner] = useState<string | null>(null)
  const [domain, setDomain] = useState('all')
  const [asOf, setAsOf] = useState(todayIso)
  const [input, setInput] = useState('')
  const [source, setSource] = useState<Citation | null>(null)
  const busy = messages.some((message) => message.role === 'pending' || message.role === 'streaming')

  const send = useCallback(
    async (textArg?: string) => {
      const text = (textArg ?? input).trim()
      if (!text || busy) return

      setInput('')
      setMessages((current) => [...current, { role: 'user', text }, { role: 'pending', asOf }])
      const started = performance.now()
      let reply: ChatMessage

      try {
        const completed: { answer?: ChatAnswer } = {}
        await streamChat({ query: text, as_of_date: asOf, ...(domain !== 'all' ? { domain } : {}) }, (event) => {
          if (event.type === 'delta') {
            setMessages((current) =>
              current.map((message) => {
                if (message.role === 'pending') return { role: 'streaming', text: event.delta, asOf: message.asOf }
                if (message.role === 'streaming') return { ...message, text: message.text + event.delta }
                return message
              }),
            )
          } else if (event.type === 'complete') {
            completed.answer = event.data
          }
        })
        const answer = completed.answer
        if (!answer) throw new Error('انقطع تدفق الإجابة قبل اكتماله.')
        reply = { role: 'assistant', answer, latencyMs: performance.now() - started }
        if (answer.citations[0] && window.innerWidth >= 1200) setSource(answer.citations[0])
      } catch (error) {
        reply = { role: 'error', text: error instanceof Error ? error.message : 'تعذّر إكمال الطلب.' }
      }

      setMessages((current) => [
        ...current.filter((message) => message.role !== 'pending' && message.role !== 'streaming'),
        reply,
      ])
    },
    [asOf, busy, domain, input],
  )

  useEffect(() => {
    // Switching accounts is an external storage change: replace, never merge, the visible history.
    // oxlint-disable-next-line react/set-state-in-effect
    setSource(null)
    if (owner === null) {
      setMessages([])
      setLoadedOwner(null)
      return
    }
    setMessages(loadChatMessages(owner))
    setLoadedOwner(owner)
  }, [owner])

  useEffect(() => {
    if (owner !== null && loadedOwner === owner) saveChatMessages(owner, messages)
  }, [loadedOwner, messages, owner])

  const clear = useCallback(() => {
    if (owner !== null) clearSavedChat(owner)
    setMessages([])
    setSource(null)
  }, [owner])

  return { messages, domain, setDomain, asOf, setAsOf, input, setInput, source, setSource, send, clear, busy }
}

export type ChatController = ReturnType<typeof useChat>

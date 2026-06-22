import { useCallback, useEffect, useRef, useState } from 'react'

interface SpeechRecognitionResultEventLike {
  resultIndex: number
  results: ArrayLike<{ isFinal: boolean; [index: number]: { transcript: string } }>
}

interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: SpeechRecognitionResultEventLike) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike

function getCtor(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor
    webkitSpeechRecognition?: SpeechRecognitionCtor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

export interface UseDictationOptions {
  onResult: (text: string, isFinal: boolean) => void
  lang?: string
}

export interface UseDictation {
  supported: boolean
  listening: boolean
  error: string | null
  start(): void
  stop(): void
  toggle(): void
}

export function useDictation({ onResult, lang = 'en-US' }: UseDictationOptions): UseDictation {
  const ctorRef = useRef<SpeechRecognitionCtor | null>(getCtor())
  const recRef = useRef<SpeechRecognitionLike | null>(null)
  const [listening, setListening] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Keep the latest onResult without rebuilding the recognition instance.
  const onResultRef = useRef(onResult)
  useEffect(() => { onResultRef.current = onResult }, [onResult])

  const supported = ctorRef.current != null

  const stop = useCallback(() => {
    recRef.current?.stop()
  }, [])

  const start = useCallback(() => {
    const Ctor = ctorRef.current
    if (!Ctor || recRef.current) return
    setError(null)
    const rec = new Ctor()
    rec.lang = lang
    rec.continuous = true
    rec.interimResults = true
    rec.onresult = (event) => {
      let interim = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const res = event.results[i]
        const transcript = res[0].transcript
        if (res.isFinal) onResultRef.current(transcript, true)
        else interim += transcript
      }
      if (interim) onResultRef.current(interim, false)
    }
    rec.onerror = (event) => { setError(event.error || 'speech-error'); setListening(false) }
    rec.onend = () => { recRef.current = null; setListening(false) }
    recRef.current = rec
    setListening(true)
    rec.start()
  }, [lang])

  const toggle = useCallback(() => {
    if (recRef.current) stop()
    else start()
  }, [start, stop])

  // Tear down any active recognition on unmount.
  useEffect(() => () => { recRef.current?.stop(); recRef.current = null }, [])

  return { supported, listening, error, start, stop, toggle }
}

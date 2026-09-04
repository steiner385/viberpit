import { useCallback, useRef, useState } from 'react'
import { useDictation, type UseDictation } from './useDictation'
import { useServerDictation } from './useServerDictation'
import { useServerSttEnabled } from './sttCapability'
import { spliceAt } from './dictationInsert'
import { autoGrow } from './autoGrow'

export interface UseDictationFieldOptions {
  value: string
  setValue: (v: string) => void
  textareaRef: React.RefObject<HTMLTextAreaElement | null>
}

type EngineKind = 'server' | 'browser'

export function useDictationField({ value, setValue, textareaRef }: UseDictationFieldOptions): UseDictation {
  // Mirror the latest value so rapid result events between renders splice correctly.
  const valueRef = useRef(value)
  // eslint-disable-next-line react-hooks/refs
  valueRef.current = value
  const anchorRef = useRef(0)   // caret index where dictation inserts
  const interimRef = useRef(0)  // length of the currently-shown interim chunk

  const onResult = useCallback((text: string, isFinal: boolean) => {
    const insert = isFinal ? `${text} ` : text
    const next = spliceAt(valueRef.current, anchorRef.current, interimRef.current, insert)
    valueRef.current = next
    if (isFinal) {
      anchorRef.current += insert.length
      interimRef.current = 0
    } else {
      interimRef.current = insert.length
    }
    setValue(next)
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => autoGrow(textareaRef.current))
    }
  }, [setValue, textareaRef])

  // Both hooks are called unconditionally (rules of hooks); only the selected one is driven.
  const browser = useDictation({ onResult })
  const server = useServerDictation({ onResult })
  const preferredKind: EngineKind = useServerSttEnabled() && server.supported ? 'server' : 'browser'

  // The engine `start()` actually drove. `preferredKind` can flip mid-dictation — e.g. the edge
  // clears `sttEnabled` on logout, which flips AppShell's `sttCapability` store — but a live
  // socket/mic on the OTHER engine does not stop itself just because the flag changed. Stay
  // pinned to whatever is running so stop()/toggle() (and the `listening`/`error` exposed below)
  // keep addressing IT, not whichever engine merely looks preferred this render.
  //
  // `useState`, not a `useRef`: the pin must be READABLE during render (to compute `dictation`
  // below), and react-hooks/refs forbids reading `.current` during render even for that. It is
  // only ever WRITTEN from the event handlers below, never from render itself.
  const [activeKind, setActiveKind] = useState<EngineKind | null>(null)
  const activeEngine = activeKind === 'server' ? server : activeKind === 'browser' ? browser : null
  const dictation = activeEngine ?? (preferredKind === 'server' ? server : browser)

  const start = useCallback(() => {
    const el = textareaRef.current
    anchorRef.current = el?.selectionStart ?? valueRef.current.length
    interimRef.current = 0
    setActiveKind(preferredKind)
    ;(preferredKind === 'server' ? server : browser).start()
  }, [preferredKind, server, browser, textareaRef])

  const stop = useCallback(() => {
    // The pinned engine already reported it stopped on its own (natural completion, error, or a
    // prior stop()) — release the pin so the NEXT start()/render picks up the current preference.
    if (activeEngine && !activeEngine.listening) setActiveKind(null)
    ;(activeEngine ?? dictation).stop()
  }, [activeEngine, dictation])

  const toggle = useCallback(() => {
    if (activeEngine && !activeEngine.listening) setActiveKind(null)
    if (dictation.listening) stop()
    else start()
  }, [activeEngine, dictation, start, stop])

  return { ...dictation, start, stop, toggle }
}

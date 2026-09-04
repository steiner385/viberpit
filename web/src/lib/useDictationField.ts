import { useCallback, useRef } from 'react'
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
  const dictation = useServerSttEnabled() && server.supported ? server : browser

  const start = useCallback(() => {
    const el = textareaRef.current
    anchorRef.current = el?.selectionStart ?? valueRef.current.length
    interimRef.current = 0
    dictation.start()
  }, [dictation, textareaRef])

  const toggle = useCallback(() => {
    if (dictation.listening) dictation.stop()
    else start()
  }, [dictation, start])

  return { ...dictation, start, toggle }
}

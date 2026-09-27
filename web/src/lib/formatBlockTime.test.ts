// @vitest-environment node
import { formatBlockTime, formatEventTime } from './formatBlockTime'

test('an event from today is a clock with seconds; from another day it carries the date', () => {
  const now = new Date(2026, 8, 27, 15, 0, 0)
  const today = formatEventTime(new Date(2026, 8, 27, 14, 32, 7).toISOString(), now)
  expect(today.short).toMatch(/\d{1,2}:\d{2}:\d{2}/)
  expect(today.short).not.toMatch(/Sep/)
  const yesterday = formatEventTime(new Date(2026, 8, 26, 14, 32, 7).toISOString(), now)
  expect(yesterday.short).toMatch(/Sep/)
  expect(yesterday.full.length).toBeGreaterThan(yesterday.short.length)
})

test('an unparseable event timestamp is shown as-is, never "Invalid Date"', () => {
  expect(formatEventTime('not a date')).toEqual({ short: 'not a date', full: 'not a date' })
})

test('absent ts yields empty strings', () => {
  expect(formatBlockTime(undefined)).toEqual({ short: '', full: '' })
})

test('present ts yields HH:MM short and a longer full form', () => {
  const { short, full } = formatBlockTime(Date.UTC(2026, 5, 23, 14, 32, 7))
  expect(short).toMatch(/\d{1,2}:\d{2}/)
  expect(full.length).toBeGreaterThan(short.length)
})

import { formatBlockTime } from './formatBlockTime'

test('absent ts yields empty strings', () => {
  expect(formatBlockTime(undefined)).toEqual({ short: '', full: '' })
})

test('present ts yields HH:MM short and a longer full form', () => {
  const { short, full } = formatBlockTime(Date.UTC(2026, 5, 23, 14, 32, 7))
  expect(short).toMatch(/\d{1,2}:\d{2}/)
  expect(full.length).toBeGreaterThan(short.length)
})

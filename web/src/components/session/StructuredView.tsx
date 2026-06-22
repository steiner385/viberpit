import { cn } from '@/lib/utils'

interface Props {
  /** Structured data — a plain object, or an array of objects (rendered as a table). */
  data: unknown
  /** Tint the container for error results. */
  ok?: boolean
}

/** Render a single scalar/nested value as a short, legible cell. Nested objects
 * and arrays are summarized (`{…}`, `[N]`) — the full data lives in the raw toggle. */
function valueText(v: unknown): string {
  if (v === null) return 'null'
  if (v === undefined) return ''
  if (Array.isArray(v)) return `[${v.length}]`
  if (typeof v === 'object') return '{…}'
  return String(v)
}

const PRE = 'mt-1 max-h-64 overflow-auto rounded bg-gray-50 p-2 text-xs font-mono text-gray-700 dark:bg-gray-900 dark:text-gray-300 md:max-h-96'

function RawDetails({ data }: { data: unknown }) {
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-xs text-gray-500 dark:text-gray-400">raw</summary>
      <pre className={PRE}>{JSON.stringify(data, null, 2)}</pre>
    </details>
  )
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v != null && typeof v === 'object' && !Array.isArray(v)
}

export function StructuredView({ data, ok = true }: Props) {
  const wrap = cn('text-xs', !ok && 'rounded bg-red-50 p-2 dark:bg-red-950/30')

  // Array of objects → table (columns from the first row's keys).
  if (Array.isArray(data) && data.length > 0 && data.every(isPlainObject)) {
    const cols = Object.keys(data[0] as Record<string, unknown>)
    return (
      <div className={wrap}>
        <table className="w-full border-collapse text-left">
          <thead>
            <tr className="border-b border-gray-200 dark:border-gray-700">
              {cols.map((c) => (
                <th key={c} className="px-2 py-1 font-semibold text-gray-600 dark:text-gray-300">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((row, i) => (
              <tr key={i} className="border-b border-gray-100 dark:border-gray-800">
                {cols.map((c) => (
                  <td key={c} className="px-2 py-1 font-mono text-gray-800 dark:text-gray-200">
                    {valueText((row as Record<string, unknown>)[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <RawDetails data={data} />
      </div>
    )
  }

  // Plain object → key/value rows.
  if (isPlainObject(data)) {
    const entries = Object.entries(data)
    return (
      <div className={wrap}>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5">
          {entries.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-semibold text-gray-600 dark:text-gray-300">{k}</dt>
              <dd className="break-words font-mono text-gray-800 dark:text-gray-200">{valueText(v)}</dd>
            </div>
          ))}
        </dl>
        <RawDetails data={data} />
      </div>
    )
  }

  // Scalar or empty — just show the raw value.
  return (
    <div className={wrap}>
      <pre className={PRE}>{JSON.stringify(data, null, 2)}</pre>
    </div>
  )
}

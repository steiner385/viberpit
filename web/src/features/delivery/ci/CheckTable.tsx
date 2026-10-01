import type { ReactNode } from 'react'

export interface Column<T> {
  header: string
  cell: (row: T) => ReactNode
  className?: string
}

export function CheckTable<T>({ title, description, columns, rows, empty, placeholder }: {
  title: string
  /** One line under the heading saying what the table counts. */
  description?: string
  columns: Column<T>[]
  rows: T[]
  empty: string
  /** Rendered in place of the rows (e.g. a loading skeleton or a fetch error)
   *  while the data is not yet usable; the heading still shows. */
  placeholder?: ReactNode
}) {
  return (
    <section className="rounded-md border border-edge bg-surface">
      <div className="border-b border-edge px-3 py-2">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {description && <p className="text-xs text-ink-muted">{description}</p>}
      </div>
      {placeholder ? (
        <div className="px-3 py-3">{placeholder}</div>
      ) : rows.length === 0 ? (
        <p className="px-3 py-3 text-xs text-ink-muted">{empty}</p>
      ) : (
        // Body text at the reading size (Read Ink: 12px is for labels, not
        // for the numbers a reader is here to judge — 2026-10-01 Delivery
        // critique, P2); the header row keeps the label size.
        <table aria-label={title} className="w-full text-sm tabular-nums">
          <thead>
            <tr className="text-left text-xs text-ink-muted">
              {columns.map((c) => (
                <th key={c.header} className={`px-3 py-1 font-medium ${c.className ?? ''}`}>
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-t border-edge">
                {columns.map((c) => (
                  <td key={c.header} className={`px-3 py-1.5 ${c.className ?? ''}`}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

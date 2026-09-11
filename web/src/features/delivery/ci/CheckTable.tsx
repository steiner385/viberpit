import type { ReactNode } from 'react'

export interface Column<T> {
  header: string
  cell: (row: T) => ReactNode
  className?: string
}

export function CheckTable<T>({ title, columns, rows, empty }: {
  title: string
  columns: Column<T>[]
  rows: T[]
  empty: string
}) {
  return (
    <section className="rounded-md border border-edge bg-surface">
      <h3 className="border-b border-edge px-3 py-2 text-sm font-semibold text-ink">
        {title}
      </h3>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-xs text-ink-muted">{empty}</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-ink-muted">
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
                  <td key={c.header} className={`px-3 py-1 ${c.className ?? ''}`}>
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

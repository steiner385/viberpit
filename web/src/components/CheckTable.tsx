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
    <section className="rounded border border-gray-200 dark:border-gray-700">
      <h3 className="px-3 py-2 text-sm font-semibold border-b border-gray-200 dark:border-gray-700">
        {title}
      </h3>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-xs text-gray-500">{empty}</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-gray-500">
              {columns.map((c) => (
                <th key={c.header} className={`px-3 py-1 font-medium ${c.className ?? ''}`}>
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
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

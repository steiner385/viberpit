import { describe, it, expect } from 'vitest'
import { NAV_DEFS } from './navDefs'
import { LIST_SECTIONS } from './listColumns'

describe('listColumns', () => {
  it('LIST_SECTIONS is exactly the set of nav sections with hasList', () => {
    // One fact, two readers: `hasList` drives the region matrix, LIST_SECTIONS
    // drives which column renders. A section that flips one without the other
    // either eats a grid column with nothing in it or renders a column the
    // matrix hides.
    expect([...LIST_SECTIONS]).toEqual(NAV_DEFS.filter((d) => d.hasList).map((d) => d.id))
  })
})

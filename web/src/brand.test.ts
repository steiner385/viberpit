import { describe, it, expect } from 'vitest'
import { BRAND_NAME, BRAND_NAME_PARTS } from './brand'

describe('brand', () => {
  it('the wordmark halves join back to the display name', () => {
    // `Wordmark` colours the halves separately; a rename that touches one
    // constant and not the other would render a name that is not the brand.
    expect(BRAND_NAME_PARTS.join('')).toBe(BRAND_NAME)
  })
})

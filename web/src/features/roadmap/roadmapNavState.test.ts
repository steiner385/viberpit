// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  roadmapScopeForSearch, roadmapPath, productPath, capabilityPath, initiativePath,
  roadmapOpenItem, keyForOpenItem, PRODUCT_ROUTE, CAPABILITY_ROUTE, INITIATIVE_ROUTE,
} from './roadmapNavState'

describe('roadmapNavState', () => {
  it('derives the scope from ?scope= and defaults to map', () => {
    expect(roadmapScopeForSearch('')).toBe('map')
    expect(roadmapScopeForSearch('?scope=initiatives')).toBe('initiatives')
    expect(roadmapScopeForSearch('?scope=bogus&x=1')).toBe('map')
  })
  it('builds every path from the section path', () => {
    expect(roadmapPath()).toBe('/roadmap')
    expect(roadmapPath('initiatives')).toBe('/roadmap?scope=initiatives')
    expect(productPath('kindash')).toBe('/roadmap/kindash')
    expect(capabilityPath('kindash', 'task views')).toBe('/roadmap/kindash/capabilities/task%20views')
    expect(initiativePath('views')).toBe('/initiatives/views')
    expect(PRODUCT_ROUTE).toBe('/roadmap/:productSlug')
    expect(CAPABILITY_ROUTE).toBe('/roadmap/:productSlug/capabilities/:capSlug')
    expect(INITIATIVE_ROUTE).toBe('/initiatives/:initiativeSlug')
  })
  it('resolves the open item from params by NAME, most specific first', () => {
    expect(roadmapOpenItem({})).toBeNull()
    expect(roadmapOpenItem({ productSlug: 'k' })).toEqual({ kind: 'product', productSlug: 'k' })
    expect(roadmapOpenItem({ productSlug: 'k', capSlug: 'c' })).toEqual({ kind: 'capability', productSlug: 'k', capSlug: 'c' })
    expect(roadmapOpenItem({ initiativeSlug: 'i' })).toEqual({ kind: 'initiative', initiativeSlug: 'i' })
    // Every kind maps to the key `treeRows` gives that row, so a URL highlights
    // the row the user actually sees.
    expect(keyForOpenItem({ kind: 'product', productSlug: 'k' })).toBe('product:k')
    expect(keyForOpenItem({ kind: 'capability', productSlug: 'k', capSlug: 'c' })).toBe('capability:k:c')
    expect(keyForOpenItem({ kind: 'initiative', initiativeSlug: 'i' })).toBe('initiative:i')
    expect(keyForOpenItem(null)).toBeNull()
  })
})

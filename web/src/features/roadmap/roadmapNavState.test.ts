// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  roadmapScopeForSearch, roadmapPath, productPath, capabilityPath, initiativePath,
  roadmapOpenItem, keyForOpenItem, PRODUCT_ROUTE, CAPABILITY_ROUTE, INITIATIVE_ROUTE,
  inboxPath, inboxProductForSearch,
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
    const sp = new URLSearchParams('')
    expect(roadmapOpenItem({}, sp)).toBeNull()
    expect(roadmapOpenItem({ productSlug: 'k' }, sp)).toEqual({ kind: 'product', productSlug: 'k' })
    expect(roadmapOpenItem({ productSlug: 'k', capSlug: 'c' }, sp)).toEqual({ kind: 'capability', productSlug: 'k', capSlug: 'c' })
    expect(roadmapOpenItem({ initiativeSlug: 'i' }, sp)).toEqual({ kind: 'initiative', initiativeSlug: 'i' })
    // Every kind maps to the key `treeRows` gives that row, so a URL highlights
    // the row the user actually sees.
    expect(keyForOpenItem({ kind: 'product', productSlug: 'k' })).toBe('product:k')
    expect(keyForOpenItem({ kind: 'capability', productSlug: 'k', capSlug: 'c' })).toBe('capability:k:c')
    expect(keyForOpenItem({ kind: 'initiative', initiativeSlug: 'i' })).toBe('initiative:i')
    expect(keyForOpenItem(null)).toBeNull()
  })
  it('knows the inbox scope and builds its paths', () => {
    expect(roadmapScopeForSearch('?scope=inbox')).toBe('inbox')
    expect(roadmapPath('inbox')).toBe('/roadmap?scope=inbox')
    expect(inboxPath({})).toBe('/roadmap?scope=inbox')
    expect(inboxPath({ product: 'kin dash' })).toBe('/roadmap?scope=inbox&product=kin%20dash')
    expect(inboxPath({ product: 'kindash', proposal: 'p1' })).toBe('/roadmap?scope=inbox&product=kindash&proposal=p1')
    expect(inboxProductForSearch('?scope=inbox&product=kindash')).toBe('kindash')
    expect(inboxProductForSearch('?scope=map&product=kindash')).toBeNull()
  })
  it('resolves an open proposal from the search, never from a route param', () => {
    const sp = (s: string) => new URLSearchParams(s)
    expect(roadmapOpenItem({}, sp('?scope=inbox&proposal=p1'))).toEqual({ kind: 'proposal', proposalId: 'p1' })
    expect(roadmapOpenItem({}, sp('?scope=map&proposal=p1'))).toBeNull()
    expect(roadmapOpenItem({ productSlug: 'k' }, sp('?scope=inbox&proposal=p1'))).toEqual({ kind: 'product', productSlug: 'k' })
    expect(keyForOpenItem({ kind: 'proposal', proposalId: 'p1' })).toBe('proposal:p1')
  })
})

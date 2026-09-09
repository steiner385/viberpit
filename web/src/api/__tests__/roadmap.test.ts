import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api/client', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

import { api } from '@/api/client'
import { fetchRoadmapMap, fetchLeaves, fetchQueue, fetchProject, ROADMAP_TIERS } from '@/api/roadmap'

const get = vi.mocked(api.get)

describe('roadmap fetchers', () => {
  beforeEach(() => get.mockReset())

  it('pins the tier vocabulary to the server order', () => {
    expect(ROADMAP_TIERS).toEqual(['needs-you', 'blocked', 'in-flight', 'proposed', 'live', 'idle'])
  })

  it('reads the map', async () => {
    get.mockResolvedValueOnce({ products: [], generatedAt: 'now' })
    await expect(fetchRoadmapMap()).resolves.toEqual({ products: [], generatedAt: 'now' })
    expect(get).toHaveBeenCalledWith('/pm/map')
  })

  it('reads leaves by exactly one scope and unwraps items', async () => {
    get.mockResolvedValueOnce({ items: [{ id: 'w1' }] })
    await expect(fetchLeaves({ capability: 'f 1' })).resolves.toEqual([{ id: 'w1' }])
    expect(get).toHaveBeenCalledWith('/pm/leaves?capability=f%201')
    get.mockResolvedValueOnce({ items: [] })
    await fetchLeaves({ initiative: 'slug' })
    expect(get).toHaveBeenLastCalledWith('/pm/leaves?initiative=slug')
  })

  it('reads the queue and one project', async () => {
    get.mockResolvedValueOnce({ entries: [], skipped: 0, generatedAt: 'now' })
    await fetchQueue()
    expect(get).toHaveBeenCalledWith('/pm/queue')
    get.mockResolvedValueOnce({ id: 'p', slug: 'a/b' })
    await fetchProject('a/b')
    expect(get).toHaveBeenLastCalledWith('/pm/projects/a%2Fb')
  })
})

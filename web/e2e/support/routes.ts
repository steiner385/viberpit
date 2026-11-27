/**
 * The top-level routes the cross-browser gates cover, and "this page has settled".
 * One list: the viewport matrix (#1412) and the adaptation gate (#1416) must not
 * drift into covering different pages.
 */
import { expect, type Page } from '@playwright/test'
import { installSeededApi } from '../fixtures/seed'

export const ROUTES = [
  { path: '/sessions', name: 'Sessions' },
  { path: '/pipeline', name: 'Pipeline' },
  { path: '/roadmap', name: 'Roadmap' },
  { path: '/delivery', name: 'Delivery' },
  { path: '/settings/general', name: 'Settings' },
] as const

export type Route = (typeof ROUTES)[number]

/**
 * Open `route` on the seeded backend and wait until it is settled, not just mounted:
 * the pages are lazy chunks plus queries, and a check run against a skeleton passes
 * for the wrong reason (and fails differently on a slower engine). An error page is
 * not a settled page either — it once stood in for two of these routes unnoticed.
 */
export async function openSeededRoute(page: Page, route: Route): Promise<void> {
  await installSeededApi(page)
  await page.goto(route.path)
  await expect(page.locator('main')).toHaveCount(1)
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0)
  await expect(page.getByText('This page hit a problem')).toHaveCount(0)
  if (route.path === '/sessions') await expect(page.getByText('Refactor the importer')).toBeVisible()
}

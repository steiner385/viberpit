import { defineConfig, devices } from '@playwright/test'

// The cross-browser gate (#1412, G14). Chromium runs every spec — it is the engine
// the product is built and mostly used on. Firefox and WebKit run only the
// representative set in `e2e/cross-browser/`, so the per-PR cost of the two extra
// engines stays bounded. The nightly workflow sets `PW_BROAD=1` to point all three
// engines at the whole suite, which is where engine-specific regressions in the
// other 30-odd specs surface without taxing every PR.
const BROAD = process.env.PW_BROAD === '1'
const REPRESENTATIVE = /e2e\/cross-browser\/.*\.spec\.ts$/

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  // CI: machine-readable results for the CI/CD Doctor (junit) and for the flake
  // measurement (json — the only reporter that says `flaky` outright, which junit
  // folds into a plain pass), plus terse console output.
  reporter: process.env.CI
    ? [
        ['list'],
        ['junit', { outputFile: 'test-results/e2e.xml' }],
        ['json', { outputFile: 'test-results/e2e.json' }],
      ]
    : 'html',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    // The trace is the evidence for a failure: screenshots, DOM and network per step.
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
      testMatch: BROAD ? undefined : REPRESENTATIVE,
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
      testMatch: BROAD ? undefined : REPRESENTATIVE,
    },
  ],
  webServer: {
    // Bind IPv4 explicitly: `vite preview` on plain `localhost` can bind only
    // ::1 on IPv6-capable hosts (Ubuntu CI runners), while Playwright polls
    // the IPv4 URL below and times out waiting for the server.
    command: 'npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})

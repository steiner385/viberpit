// Trusted synthetic fixtures only. This calibrates acceptance checks, not agent efficacy.
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';

const report = {
  schema_version: 1,
  report_kind: 'ui-acceptance-calibration',
  executed: false,
  passed: false,
  browser: { name: 'chromium', version: '' },
  fixtures: [],
};
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
let browser;
let finished = false;
function finish() {
  if (finished) return;
  finished = true;
  process.stdout.write(`${JSON.stringify(report)}\n`);
  process.exitCode = report.passed ? 0 : 1;
}
const deadline = setTimeout(async () => {
  report.passed = false;
  report.error = 'Calibration exceeded 120 second deadline';
  finish();
  setTimeout(() => process.exit(1), 5000).unref();
  await browser?.close().catch(() => {});
  process.exit(1);
}, 120_000);

try {
  // Import inside try so a missing dependency still produces a structured failure.
  const { chromium } = await import('@playwright/test');
  const manifestBytes = await readFile(new URL('./manifest.json', import.meta.url));
  const manifest = JSON.parse(manifestBytes);
  report.manifest_sha256 = digest(manifestBytes);
  report.fixture_revision = manifest.revision;
  if (manifest.schema_version !== 1 || manifest.fixtures.length !== 8) {
    throw new Error('Unexpected calibration manifest');
  }
  browser = await chromium.launch({ timeout: 15_000 });
  report.browser.version = browser.version();
  report.executed = true;
  for (const fixture of manifest.fixtures) {
    const bytes = await readFile(new URL(fixture.file, import.meta.url));
    const result = {
      id: fixture.id,
      sha256: digest(bytes),
      expected_failures: fixture.expected_failures,
      observed_failures: [],
      passed: false,
    };
    report.fixtures.push(result);
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      reducedMotion: 'no-preference',
      serviceWorkers: 'block',
      offline: true,
    });
    try {
      await context.route('**/*', route => route.abort());
      const page = await context.newPage();
      page.setDefaultTimeout(3000);
      await page.setContent(bytes.toString('utf8'), { waitUntil: 'domcontentloaded' });
      const input = page.locator('#note');
      const save = page.locator('#save');
      // Missing selectors must fail the run, not turn into zero findings.
      for (const selector of ['#note', '#save', '#error', '#result', '#activity']) {
        if (await page.locator(selector).count() !== 1) throw new Error(`Expected one ${selector}`);
      }
      await input.focus();
      await page.keyboard.press('Tab');
      if (!await save.evaluate(element => element === document.activeElement)) {
        result.observed_failures.push('keyboard-order');
      } else {
        await page.keyboard.press('Enter');
        if (await page.locator('#error').textContent() !== 'Enter a note') {
          result.observed_failures.push('keyboard-order');
        }
      }
      // Exercise real input and submit events: invalid submission then successful save.
      await input.fill('');
      await save.click();
      const invalid = await page.locator('#error').textContent() === 'Enter a note'
        && await page.locator('#result').textContent() === '';
      await input.fill('  Calibration note  ');
      await save.click();
      const valid = await page.locator('#result').textContent() === 'Saved: Calibration note'
        && await page.locator('#error').textContent() === '';
      if (!invalid || !valid) result.observed_failures.push('functional-behavior');
      if (await page.getByRole('textbox', { name: 'Note', exact: true }).count() !== 1) {
        result.observed_failures.push('accessible-name');
      }
      const layout = await page.evaluate(() => ({
        width: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      if (layout.scrollWidth > layout.width) result.observed_failures.push('mobile-overflow');
      const target = await save.boundingBox();
      if (!target || target.width < 44 || target.height < 44) result.observed_failures.push('touch-target');
      const normalAnimation = await page.locator('#activity').evaluate(element => getComputedStyle(element).animationName);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      const reducedAnimation = await page.locator('#activity').evaluate(element => getComputedStyle(element).animationName);
      if (normalAnimation === 'none' || reducedAnimation !== 'none') result.observed_failures.push('reduced-motion');
      result.measurements = { invalid_submission: invalid, valid_submission: valid, layout,
        target: target && { width: target.width, height: target.height }, normalAnimation, reducedAnimation };
      result.passed = JSON.stringify([...result.observed_failures].sort())
        === JSON.stringify([...result.expected_failures].sort());
    } finally {
      await context.close();
    }
  }
  report.passed = report.fixtures.every(fixture => fixture.passed);
} catch (error) {
  report.error = error instanceof Error ? error.message : String(error);
} finally {
  try { await browser?.close(); } catch (error) {
    report.passed = false;
    report.error = `Browser cleanup failed: ${error.message}`;
  }
  clearTimeout(deadline);
  finish();
}

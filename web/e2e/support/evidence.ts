/**
 * Publish a file as test evidence (#1412, G14).
 *
 * `testInfo.attach(name, { body })` keeps the bytes in memory for the HTML reporter
 * only: under CI's `list`/`junit`/`json` reporters a PASSING test's body attachment
 * never reaches disk, so the artifact upload would silently contain nothing for the
 * green matrix cells — exactly the screenshots the evidence is meant to show.
 * Writing into the test's own output directory and attaching by `path` is what
 * `preserveOutput: 'always'` (the default) keeps and the CI job uploads.
 */
import { writeFile } from 'node:fs/promises'
import type { TestInfo } from '@playwright/test'

export async function publishEvidence(
  testInfo: TestInfo,
  fileName: string,
  body: Buffer | string,
  contentType: string,
): Promise<void> {
  const path = testInfo.outputPath(fileName)
  await writeFile(path, body)
  await testInfo.attach(fileName, { path, contentType })
}

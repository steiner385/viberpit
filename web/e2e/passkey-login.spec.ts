/**
 * Passkey sign-in, end to end in a real Chromium (#1410 AC-G12.11).
 *
 * A CDP virtual authenticator (CTAP2, platform, resident keys, user
 * verification) holds one discoverable credential whose private key this spec
 * generated, so the browser runs a REAL `navigator.credentials.get` ceremony
 * against the page. The API is stubbed (as in every spec here): an
 * unauthenticated `/api/*` answers the server's passkey 401, and the spec plays
 * the server half of begin/complete — then checks the posted assertion the way
 * the server would: base64url fields, the right challenge and origin in
 * clientDataJSON, the credential id, the user handle, and a signature that
 * verifies against the credential's public key.
 *
 * WebAuthn rejects an IP-address RP ID, so this spec drives `localhost` rather
 * than the configured `127.0.0.1` base URL; Chromium resolves both to the same
 * preview server.
 *
 * Run: npx playwright test e2e/passkey-login.spec.ts --project=chromium
 */
import { createHash, generateKeyPairSync, randomBytes, verify, type KeyObject } from 'node:crypto'
import { test, expect, type Page, type Request } from '@playwright/test'

test.skip(({ browserName }) => browserName !== 'chromium', 'The virtual authenticator is a Chromium CDP domain.')

const ORIGIN = 'http://localhost:4173'
const RP_ID = 'localhost'
const IDENTITY = 'alice@example.com'
const PASSKEY_401 = { error: { code: 'UNAUTHORIZED', message: 'No valid identity', login: 'passkey' } }

const b64url = (buf: Buffer) => buf.toString('base64url')
const fromB64url = (s: string) => Buffer.from(s, 'base64url')

interface Authenticator {
  credentialId: Buffer
  userHandle: Buffer
  publicKey: KeyObject
}

async function addVirtualAuthenticator(page: Page): Promise<Authenticator> {
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  })
  const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' })
  const credentialId = randomBytes(16)
  const userHandle = Buffer.from('user-alice')
  await cdp.send('WebAuthn.addCredential', {
    authenticatorId,
    credential: {
      credentialId: credentialId.toString('base64'),
      isResidentCredential: true,
      rpId: RP_ID,
      privateKey: privateKey.export({ format: 'der', type: 'pkcs8' }).toString('base64'),
      userHandle: userHandle.toString('base64'),
      signCount: 0,
    },
  })
  return { credentialId, userHandle, publicKey }
}

interface Server {
  challenge: Buffer
  completes: Request[]
  /** What `complete` answers. */
  completeStatus: number
}

/** The server half: 401 until `complete` succeeds, then a minimal signed-in API. */
async function stubServer(page: Page): Promise<Server> {
  const server: Server = { challenge: randomBytes(32), completes: [], completeStatus: 200 }
  let signedIn = false
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/webauthn/login/begin') {
      return route.fulfill({ json: {
        ceremonyId: 'cer-1',
        publicKey: {
          challenge: b64url(server.challenge), rpId: RP_ID, timeout: 60_000, userVerification: 'required',
        },
      } })
    }
    if (path === '/api/auth/webauthn/login/complete') {
      server.completes.push(route.request())
      if (server.completeStatus !== 200) {
        return route.fulfill({ status: server.completeStatus, json: { error: { code: 'UNAUTHORIZED' } } })
      }
      signedIn = true
      return route.fulfill({ json: { identity: IDENTITY, authMode: 'passkey' } })
    }
    if (!signedIn) return route.fulfill({ status: 401, json: PASSKEY_401 })
    if (path === '/api/auth/me') return route.fulfill({ json: { identity: IDENTITY, scope: 'full', authMode: 'passkey' } })
    if (path === '/api/config') return route.fulfill({ json: { repoRoot: '', adoptionEnabled: false, webauthnEnabled: true } })
    return route.fulfill({ json: [] })
  })
  return server
}

test('an unauthenticated visit lands on /login and a passkey signs in', async ({ page }) => {
  const auth = await addVirtualAuthenticator(page)
  const server = await stubServer(page)

  await page.goto(`${ORIGIN}/pipeline`)
  await expect(page).toHaveURL(`${ORIGIN}/login?next=%2Fpipeline`)

  await page.getByRole('button', { name: 'Sign in with a passkey' }).click()
  await expect(page).toHaveURL(`${ORIGIN}/pipeline`)

  expect(server.completes).toHaveLength(1)
  const body = server.completes[0].postDataJSON() as {
    ceremonyId: string
    credential: {
      id: string; rawId: string; type: string
      response: { clientDataJSON: string; authenticatorData: string; signature: string; userHandle?: string }
      clientExtensionResults: object
    }
  }
  const { credential } = body
  expect(body.ceremonyId).toBe('cer-1')
  expect(credential.type).toBe('public-key')
  expect(credential.id).toBe(b64url(auth.credentialId))
  expect(credential.rawId).toBe(b64url(auth.credentialId))
  expect(credential.clientExtensionResults).toEqual({})
  for (const v of Object.values(credential.response)) expect(v).toMatch(/^[A-Za-z0-9_-]+$/)
  expect(credential.response.userHandle).toBe(b64url(auth.userHandle))

  const clientDataRaw = fromB64url(credential.response.clientDataJSON)
  const clientData = JSON.parse(clientDataRaw.toString('utf8')) as { type: string; challenge: string; origin: string }
  expect(clientData).toMatchObject({ type: 'webauthn.get', challenge: b64url(server.challenge), origin: ORIGIN })

  const authData = fromB64url(credential.response.authenticatorData)
  expect(authData.subarray(0, 32)).toEqual(createHash('sha256').update(RP_ID).digest())
  // Flags: user present (0x01) and user verified (0x04).
  expect(authData[32] & 0x05).toBe(0x05)

  const signed = Buffer.concat([authData, createHash('sha256').update(clientDataRaw).digest()])
  expect(verify('sha256', signed, auth.publicKey, fromB64url(credential.response.signature))).toBe(true)
})

test('a refused passkey is reported on the login page', async ({ page }) => {
  await addVirtualAuthenticator(page)
  const server = await stubServer(page)
  server.completeStatus = 401

  await page.goto(`${ORIGIN}/login`)
  await page.getByRole('button', { name: 'Sign in with a passkey' }).click()
  await expect(page.getByRole('alert')).toHaveText('That passkey was not accepted.')
  await expect(page).toHaveURL(`${ORIGIN}/login`)
})

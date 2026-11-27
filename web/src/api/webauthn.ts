/**
 * Passkey sign-in and enrolment (#1410 FR-G12.12) — the browser half of the
 * WebAuthn ceremonies the server runs under `/api/auth/webauthn/*`.
 *
 * Each ceremony is begin → `navigator.credentials` → complete. The server
 * speaks the W3C JSON forms (`PublicKeyCredential{Request,Creation}OptionsJSON`
 * in, `{Authentication,Registration}ResponseJSON` out), with every binary field
 * base64url; this module converts both ways by hand rather than through
 * `PublicKeyCredential.parse*OptionsFromJSON` / `toJSON()`, which not every
 * supported browser ships.
 *
 * Errors pass through untouched: an `ApiRequestError` for an HTTP refusal
 * (401 not accepted, 400 stale ceremony, 429 with `retryAfterS`) and the
 * browser's `DOMException` (`NotAllowedError` = dismissed or timed out).
 *
 * Deliberately free of React Query: `/login` is its own lazy chunk and must not
 * pull the query core off the entry's shared chunk. The credential-list hooks
 * live in `passkeys.ts`.
 */
import { base64urlToBytes, bytesToBase64url } from '@/lib/base64url'
import { api } from './client'

/** A row of `GET /api/auth/credentials`: one of the caller's unrevoked passkeys. */
export interface PasskeyCredential {
  id: string
  label: string | null
  transports: string[]
  createdAt: string
  lastUsedAt: string | null
}

export interface PasskeyLoginResult { identity: string; authMode: 'passkey' }

interface CredentialDescriptorJSON {
  type: 'public-key'
  id: string
  transports?: string[]
}

interface RequestOptionsJSON {
  challenge: string
  rpId?: string
  timeout?: number
  userVerification?: UserVerificationRequirement
  allowCredentials?: CredentialDescriptorJSON[]
}

interface CreationOptionsJSON {
  challenge: string
  rp: PublicKeyCredentialRpEntity
  user: { id: string; name: string; displayName: string }
  pubKeyCredParams: PublicKeyCredentialParameters[]
  timeout?: number
  excludeCredentials?: CredentialDescriptorJSON[]
  authenticatorSelection?: AuthenticatorSelectionCriteria
  attestation?: AttestationConveyancePreference
}

interface Ceremony<T> { ceremonyId: string; publicKey: T }

/** Whether this browser can run a WebAuthn ceremony at all. */
export function passkeysSupported(): boolean {
  return typeof window !== 'undefined'
    && typeof window.PublicKeyCredential !== 'undefined'
    && typeof navigator.credentials?.get === 'function'
}

function descriptors(list: CredentialDescriptorJSON[] | undefined): PublicKeyCredentialDescriptor[] | undefined {
  return list?.map((d) => ({
    type: d.type,
    id: base64urlToBytes(d.id),
    ...(d.transports ? { transports: d.transports as AuthenticatorTransport[] } : {}),
  }))
}

function requestOptions(json: RequestOptionsJSON): PublicKeyCredentialRequestOptions {
  return { ...json, challenge: base64urlToBytes(json.challenge), allowCredentials: descriptors(json.allowCredentials) }
}

function creationOptions(json: CreationOptionsJSON): PublicKeyCredentialCreationOptions {
  return {
    ...json,
    challenge: base64urlToBytes(json.challenge),
    user: { ...json.user, id: base64urlToBytes(json.user.id) },
    excludeCredentials: descriptors(json.excludeCredentials),
  }
}

/** The fields both response JSON forms share. */
function credentialJSON(cred: PublicKeyCredential, response: Record<string, unknown>) {
  return {
    id: cred.id,
    rawId: bytesToBase64url(cred.rawId),
    type: cred.type,
    response,
    clientExtensionResults: cred.getClientExtensionResults(),
    ...(cred.authenticatorAttachment ? { authenticatorAttachment: cred.authenticatorAttachment } : {}),
  }
}

function assertionJSON(cred: PublicKeyCredential) {
  const r = cred.response as AuthenticatorAssertionResponse
  return credentialJSON(cred, {
    clientDataJSON: bytesToBase64url(r.clientDataJSON),
    authenticatorData: bytesToBase64url(r.authenticatorData),
    signature: bytesToBase64url(r.signature),
    ...(r.userHandle ? { userHandle: bytesToBase64url(r.userHandle) } : {}),
  })
}

function attestationJSON(cred: PublicKeyCredential) {
  const r = cred.response as AuthenticatorAttestationResponse
  const transports = typeof r.getTransports === 'function' ? r.getTransports() : undefined
  return credentialJSON(cred, {
    clientDataJSON: bytesToBase64url(r.clientDataJSON),
    attestationObject: bytesToBase64url(r.attestationObject),
    ...(transports ? { transports } : {}),
  })
}

/** Drop a blank optional string so the server sees the key absent, not `""`. */
function present(value: string | undefined): string | undefined {
  const v = value?.trim()
  return v ? v : undefined
}

/**
 * Sign in with a passkey. `identity` narrows the prompt to that user's
 * credentials; omitted, the browser offers every discoverable passkey for this
 * site. Resolves once the server has set the session cookie.
 */
export async function startPasskeyLogin(identity?: string): Promise<PasskeyLoginResult> {
  const who = present(identity)
  const begin = await api.post<Ceremony<RequestOptionsJSON>>(
    '/auth/webauthn/login/begin', who ? { identity: who } : {})
  const cred = await navigator.credentials.get({ publicKey: requestOptions(begin.publicKey) }) as PublicKeyCredential | null
  if (!cred) throw new DOMException('No passkey was chosen.', 'NotAllowedError')
  return api.post<PasskeyLoginResult>('/auth/webauthn/login/complete', {
    ceremonyId: begin.ceremonyId,
    credential: assertionJSON(cred),
  })
}

/** Create a passkey for the signed-in user and register it with the server. */
export async function enrolPasskey(label?: string): Promise<PasskeyCredential> {
  const name = present(label)
  const labelled = name ? { label: name } : {}
  const begin = await api.post<Ceremony<CreationOptionsJSON>>('/auth/webauthn/register/begin', labelled)
  const cred = await navigator.credentials.create({ publicKey: creationOptions(begin.publicKey) }) as PublicKeyCredential | null
  if (!cred) throw new DOMException('No passkey was created.', 'NotAllowedError')
  return api.post<PasskeyCredential>('/auth/webauthn/register/complete', {
    ceremonyId: begin.ceremonyId,
    ...labelled,
    credential: attestationJSON(cred),
  })
}


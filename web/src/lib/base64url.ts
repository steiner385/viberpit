/**
 * base64url (RFC 4648 §5, unpadded) ⇄ bytes — the one codec for the binary
 * fields the server sends as text: the push VAPID key and every WebAuthn
 * challenge, credential id and response field (#1410).
 *
 * Decoding returns a `Uint8Array` over its OWN `ArrayBuffer`, so it is a valid
 * `BufferSource` for `navigator.credentials` and `pushManager.subscribe`.
 */
export function base64urlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (value.length % 4)) % 4)
  const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'))
  const out = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i)
  return out
}

export function bytesToBase64url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes = value instanceof ArrayBuffer
    ? new Uint8Array(value)
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  let raw = ''
  for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i])
  return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

// web/src/components/session/heicConvert.ts
// Client-side HEIC/HEIF → JPEG conversion. iPhones capture HEIC by default,
// which neither the attachment server (png/jpeg/webp/gif only) nor Claude can
// consume, so we convert in the browser before upload. heic2any (libheif wasm)
// is dynamic-imported so its payload stays out of the main bundle.

const HEIC_TYPES = ['image/heic', 'image/heif']
const HEIC_EXT = /\.(heic|heif)$/i

export function isHeic(file: File): boolean {
  return HEIC_TYPES.includes(file.type.toLowerCase()) || HEIC_EXT.test(file.name)
}

export async function maybeConvertHeic(file: File): Promise<File> {
  if (!isHeic(file)) return file
  const { default: heic2any } = await import('heic2any')
  const out = await heic2any({ blob: file, toType: 'image/jpeg', quality: 0.9 })
  const blob = Array.isArray(out) ? out[0] : out
  const base = file.name.replace(HEIC_EXT, '')
  return new File([blob], `${base}.jpg`, { type: 'image/jpeg' })
}

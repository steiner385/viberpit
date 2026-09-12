// ccswitch/no-raw-palette — bans Tailwind palette literals, dark: variants, hex
// colors, z-index literals and arbitrary pixel sizes inside class strings.
// Scope is `src/**` minus the chart allowlist (row 7f, 2026-09), applied in
// eslint.config.js. Rationale: docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md §2.5.
//
// Auto-fixable (meta.fixable = 'code'): MAPPING is the sweep's codemod table —
// single source of truth, copied verbatim from
// docs/superpowers/plans/2026-09-11-finish-line-row-7-sweep.md §Mapping. A
// class string whose rewriteClassString(text) output differs from the input
// gets ONE fix per class-owning node replacing the whole string. Tokens
// MAPPING does not know remain errors with no fix — later PRs hand-map them
// per the plan's "Hand-mapping rules for residuals" paragraph.
const PALETTE =
  /^(bg|text|border|ring|fill|stroke|from|to|via|outline|decoration|divide|caret|shadow|placeholder)-(gray|slate|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}(\/\d+)?$/
const Z_LITERAL = /^z-(\[.*\]|\d+)$/
const PX_LITERAL = /^(text|min-h|min-w|max-h|max-w|h|w|p[xy]?|m[xy]?|gap)-\[\d+px\]$/
const HEX = /\[#[0-9a-fA-F]{3,8}\]/
// Fills whose ink is a token, never `text-white`: --t-accent flips with the
// theme (white on the dark-mode #5c9bff is ~2.7:1), and the -strong fills are
// theme-invariant precisely so `--t-on-strong` stays AA on them. Deliberately
// excludes the washes (`bg-accent-wash` etc.), which carry ink-tier text.
const INK_FILL = /^bg-(accent|(?:success|warning|danger|info|neutral)-strong)(\/\d+)?$/

const CLASS_CALLS = new Set(['cn', 'cva', 'clsx', 'twMerge'])

// The fixer table (§Mapping). Keys are the LIGHT class (any variant prefix
// such as hover:/focus-visible:/placeholder: is preserved on the output, as is
// a `!` important marker). A dark: token is deleted when a light token with the
// SAME partner key (see partnerKey) mapped; a dark: token with no such partner
// is left exactly as written and stays an unfixable error — a hand-mapping
// residual. `null` means "known, but not safe to auto-fix generically" — see
// the plan's hand-mapping rules.
export const MAPPING = {
  // text (ink tiers)
  'text-gray-900': 'text-ink', 'text-gray-800': 'text-ink', 'text-gray-700': 'text-ink',
  'text-gray-100': 'text-ink', 'text-gray-200': 'text-ink', 'text-gray-300': 'text-ink', // dark-side partners
  'text-gray-600': 'text-ink-muted', 'text-gray-500': 'text-ink-muted', 'text-gray-400': 'text-ink-muted',
  'text-slate-500': 'text-ink-muted', 'text-slate-400': 'text-ink-muted',
  'text-white': null, // untouched unless the fill is accent/-strong (hand-mapped)
  // surfaces
  'bg-white': 'bg-surface', 'bg-gray-800': 'bg-surface',
  'bg-gray-50': 'bg-canvas', 'bg-gray-900': 'bg-canvas',
  'bg-gray-100': 'bg-surface-raised', 'bg-gray-200': 'bg-surface-raised', 'bg-gray-700': 'bg-surface-raised', 'bg-gray-600': 'bg-surface-raised',
  // edges
  'border-gray-200': 'border-edge', 'border-gray-700': 'border-edge', 'divide-gray-200': 'divide-edge', 'divide-gray-700': 'divide-edge',
  'border-gray-300': 'border-edge-strong', 'border-gray-600': 'border-edge-strong',
  // accent (blue)
  'text-blue-600': 'text-accent', 'text-blue-400': 'text-accent', 'text-blue-500': 'text-accent', 'text-blue-300': 'text-accent', 'text-blue-700': 'text-accent',
  'bg-blue-600': 'bg-accent', 'bg-blue-500': 'bg-accent', 'bg-blue-700': 'bg-accent/90',
  'bg-blue-50': 'bg-accent-wash', 'bg-blue-900/30': 'bg-accent-wash', 'bg-blue-900/20': 'bg-accent-wash', 'bg-blue-100': 'bg-accent-wash',
  'border-blue-400': 'border-accent', 'border-blue-500': 'border-accent', 'border-blue-300': 'border-accent', 'border-blue-600': 'border-accent',
  'ring-blue-500': 'ring-accent', 'ring-blue-400': 'ring-accent', 'ring-blue-600': 'ring-accent',
  // danger (red)
  'text-red-600': 'text-danger', 'text-red-400': 'text-danger', 'text-red-700': 'text-danger', 'text-red-300': 'text-danger', 'text-red-500': 'text-danger', 'text-red-800': 'text-danger',
  'bg-red-50': 'bg-danger-wash', 'bg-red-950/30': 'bg-danger-wash', 'bg-red-900/30': 'bg-danger-wash', 'bg-red-950': 'bg-danger-wash', 'bg-red-100': 'bg-danger-wash',
  'bg-red-600': 'bg-danger-strong', 'bg-red-500': 'bg-danger-strong', 'bg-red-700': 'bg-danger-strong/90',
  'border-red-300': 'border-danger', 'border-red-700': 'border-danger', 'border-red-500': 'border-danger', 'border-red-400': 'border-danger', 'border-red-800': 'border-danger',
  'ring-red-500': 'ring-danger',
  // warning (amber / yellow)
  'text-amber-600': 'text-warning', 'text-amber-400': 'text-warning', 'text-amber-700': 'text-warning', 'text-amber-300': 'text-warning', 'text-amber-500': 'text-warning', 'text-amber-800': 'text-warning', 'text-amber-200': 'text-warning',
  'text-yellow-600': 'text-warning', 'text-yellow-400': 'text-warning', 'text-yellow-700': 'text-warning', 'text-yellow-300': 'text-warning',
  'bg-amber-50': 'bg-warning-wash', 'bg-amber-900/40': 'bg-warning-wash', 'bg-amber-900/30': 'bg-warning-wash', 'bg-amber-900/20': 'bg-warning-wash', 'bg-amber-100': 'bg-warning-wash', 'bg-yellow-50': 'bg-warning-wash', 'bg-yellow-900/30': 'bg-warning-wash',
  'bg-amber-500': 'bg-warning-strong', 'bg-amber-600': 'bg-warning-strong',
  'border-amber-300': 'border-warning', 'border-amber-600': 'border-warning', 'border-amber-700': 'border-warning', 'border-amber-400': 'border-warning', 'border-amber-500': 'border-warning', 'border-yellow-300': 'border-warning', 'border-yellow-700': 'border-warning',
  'ring-amber-500': 'ring-warning',
  // success (green / emerald)
  'text-green-600': 'text-success', 'text-green-400': 'text-success', 'text-green-700': 'text-success', 'text-green-300': 'text-success', 'text-green-500': 'text-success',
  'text-emerald-600': 'text-success', 'text-emerald-400': 'text-success', 'text-emerald-700': 'text-success', 'text-emerald-500': 'text-success',
  'bg-green-50': 'bg-success-wash', 'bg-green-900/30': 'bg-success-wash', 'bg-green-100': 'bg-success-wash', 'bg-emerald-50': 'bg-success-wash', 'bg-emerald-900/30': 'bg-success-wash',
  'bg-green-600': 'bg-success-strong', 'bg-green-500': 'bg-success-strong', 'bg-emerald-500': 'bg-success-strong', 'bg-emerald-600': 'bg-success-strong',
  'border-green-300': 'border-success', 'border-green-700': 'border-success', 'border-green-500': 'border-success', 'border-emerald-500': 'border-success',
  // info (sky / indigo / violet / purple)
  'text-sky-600': 'text-info', 'text-sky-400': 'text-info', 'text-indigo-600': 'text-info', 'text-indigo-400': 'text-info', 'text-indigo-300': 'text-info',
  'text-violet-600': 'text-info', 'text-violet-400': 'text-info', 'text-violet-300': 'text-info', 'text-purple-600': 'text-info', 'text-purple-400': 'text-info', 'text-purple-300': 'text-info',
  'bg-sky-50': 'bg-info-wash', 'bg-indigo-50': 'bg-info-wash', 'bg-violet-50': 'bg-info-wash', 'bg-purple-50': 'bg-info-wash', 'bg-violet-900/30': 'bg-info-wash', 'bg-indigo-900/30': 'bg-info-wash', 'bg-purple-900/30': 'bg-info-wash',
  'bg-violet-600': 'bg-info-strong', 'bg-indigo-600': 'bg-info-strong', 'bg-purple-600': 'bg-info-strong',
  'border-violet-300': 'border-info', 'border-violet-700': 'border-info', 'border-indigo-300': 'border-info', 'border-indigo-700': 'border-info', 'border-purple-300': 'border-info', 'border-purple-700': 'border-info',
  // layers and sizes
  'z-10': 'z-raised', 'z-20': 'z-sticky', 'z-30': 'z-popover', 'z-40': 'z-overlay', 'z-50': 'z-dialog',
  'min-h-[44px]': 'min-h-target', 'min-w-[44px]': 'min-w-target', 'h-[44px]': 'h-target', 'w-[44px]': 'w-target',
  'text-[10px]': 'text-2xs', 'text-[11px]': 'text-2xs', 'text-[12px]': 'text-xs', 'text-[13px]': 'text-sm',
}

/** Split `hover:focus-visible:bg-gray-100` into ['hover:focus-visible:', 'bg-gray-100']. */
function splitVariants(token) {
  const i = token.lastIndexOf(':')
  return i === -1 ? ['', token] : [token.slice(0, i + 1), token.slice(i + 1)]
}

// Interaction-state variants: a hover/focus/active class onto `bg-canvas` is a
// no-op on a canvas-backed page (the element already sits on that colour), so
// the fixer bumps it a tier to `bg-surface-raised` instead of following the
// light-state `bg-gray-50`/`bg-gray-900` → `bg-canvas` mapping verbatim.
const STATE_VARIANTS = new Set(['hover', 'focus', 'focus-visible', 'active'])

/** Does this token's (non-dark) variant chain contain an interaction-state variant? */
function hasStateVariant(variants) {
  return variants.split(':').some((p) => STATE_VARIANTS.has(p))
}

/**
 * Split a Tailwind important marker off a base: `!bg-gray-100` (v3, leading) or
 * `bg-gray-100!` (v4, trailing) → ['!', 'bg-gray-100', ''] / ['', 'bg-gray-100', '!'].
 * The marker is invisible to the PALETTE/MAPPING lookups and re-attached on output.
 */
function splitImportant(rawBase) {
  const lead = rawBase.startsWith('!') ? '!' : ''
  let base = rawBase.slice(lead.length)
  const trail = base.endsWith('!') ? '!' : ''
  if (trail) base = base.slice(0, -1)
  return [lead, base, trail]
}

/**
 * A token's PARTNER KEY: its non-`dark` variant chain plus the utility prefix
 * (the first segment of the base). `hover:bg-gray-100` and
 * `dark:hover:bg-gray-700` both key to `hover:|bg`; `text-gray-500` keys to
 * `|text`. Two tokens share a key exactly when they set the same CSS property
 * under the same conditions — i.e. when one is the other's theme partner.
 */
function partnerKey(variants, base) {
  const chain = variants.split(':').filter((p) => p && p !== 'dark').map((p) => p + ':').join('')
  return chain + '|' + base.split('-')[0]
}

/**
 * Only COLOUR tokens take part in light/dark partnering. The size/layer
 * entries in MAPPING (`text-[11px]`, `min-h-[44px]`, `z-50`) share a utility
 * prefix with colour tokens, and without this guard a mapped `text-[11px]`
 * would publish the `|text` key and silently delete an unrelated
 * `dark:text-gray-400` whose light side never mapped.
 */
function isColorBase(base) {
  return PALETTE.test(base) || base === 'bg-white' || base === 'text-white'
}

/** The partner keys of the light tokens in `text` that MAPPING would rewrite. */
function lightKeysOf(text) {
  const keys = new Set()
  for (const raw of text.split(/\s+/).filter(Boolean)) {
    const [variants, rawBase] = splitVariants(raw)
    if (variants.split(':').includes('dark')) continue
    const [, base] = splitImportant(rawBase)
    if (MAPPING[base] && isColorBase(base)) keys.add(partnerKey(variants, base))
  }
  return keys
}

/**
 * Pure codemod: rewrite a class string through MAPPING.
 *
 * `externalLightKeys` (optional) seeds the partner-key set with keys found in
 * strings that are applied ALONGSIDE this one — sibling quasis of the same
 * template literal, and unconditional sibling arguments of the enclosing
 * cn()/cva()/clsx()/twMerge() call. See `externalKeysFor`.
 *
 * A `dark:` token is dropped iff a light token with the same partner key
 * mapped; otherwise it is emitted VERBATIM. `dark:` is never stripped: the dark
 * value is not the light value, so rewriting `dark:bg-blue-700` to `bg-accent/90`
 * would apply the dark treatment in light mode (and, under twMerge's last-wins,
 * clobber the light token that preceded it).
 *
 * Interior whitespace collapses to a single space; a leading/trailing
 * whitespace run survives only if the rewritten body is non-empty. An emptied
 * chunk yields '' — or, with `emptyAsSeparator` (template quasis, see
 * `computeRewrite`), a single space, so the interpolations on either side of it
 * do not glue together into one nonsense class.
 */
export function rewriteClassString(text, externalLightKeys, { emptyAsSeparator = false } = {}) {
  const tokens = text.split(/\s+/).filter(Boolean)
  const mappedLightKeys = new Set(externalLightKeys)
  const out = []
  const darkAt = []
  let changed = false
  // pass 1: light tokens map in place; each mapped token publishes its partner
  // key. dark: tokens hold their slot so the output keeps source order.
  for (const raw of tokens) {
    const [variants, rawBase] = splitVariants(raw)
    if (variants.split(':').includes('dark')) { darkAt.push(out.length); out.push(raw); continue }
    const [lead, base, trail] = splitImportant(rawBase)
    const to = MAPPING[base]
    if (to) {
      const mappedTo = to === 'bg-canvas' && hasStateVariant(variants) ? 'bg-surface-raised' : to
      out.push(variants + lead + mappedTo + trail)
      if (isColorBase(base)) mappedLightKeys.add(partnerKey(variants, base))
      changed = true
    }
    else out.push(raw)
  }
  // pass 2: a dark: COLOUR token is dropped iff its light partner mapped;
  // otherwise it stays exactly as written (a residual for the hand-mapping
  // pass). Non-colour dark: tokens are never dropped.
  for (const i of darkAt) {
    const [variants, rawBase] = splitVariants(out[i])
    const [, base] = splitImportant(rawBase)
    if (isColorBase(base) && mappedLightKeys.has(partnerKey(variants, base))) { out[i] = null; changed = true }
  }
  // dedupe while keeping order
  const seen = new Set()
  const deduped = out.filter((t) => t !== null && (seen.has(t) ? false : (seen.add(t), true)))
  const leadingWs = /^\s/.test(text) ? ' ' : ''
  const trailingWs = /\s$/.test(text) ? ' ' : ''
  const body = deduped.join(' ')
  const empty = emptyAsSeparator && /\s/.test(text) ? ' ' : ''
  const outStr = body ? leadingWs + body + trailingWs : empty
  return { out: outStr, changed: changed || outStr !== text }
}

function inClassContext(node, sourceCode) {
  for (const a of sourceCode.getAncestors(node)) {
    if (a.type === 'JSXAttribute' && a.name && a.name.name === 'className') return true
    if (a.type === 'CallExpression' && a.callee.type === 'Identifier' && CLASS_CALLS.has(a.callee.name)) return true
  }
  return false
}

/** The innermost cn()/cva()/clsx()/twMerge() call enclosing `node`, or null. */
function enclosingClassCall(node, sourceCode) {
  const ancestors = sourceCode.getAncestors(node)
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const a = ancestors[i]
    if (a.type === 'CallExpression' && a.callee.type === 'Identifier' && CLASS_CALLS.has(a.callee.name)) return a
  }
  return null
}

/** Every light partner key from a string-ish node (Literal or TemplateLiteral). */
function keysFromArgument(arg, into) {
  if (arg.type === 'Literal' && typeof arg.value === 'string') {
    for (const k of lightKeysOf(arg.value)) into.add(k)
  } else if (arg.type === 'TemplateLiteral') {
    for (const q of arg.quasis) for (const k of lightKeysOf(q.value.cooked ?? '')) into.add(k)
  }
}

/**
 * Partner keys from the strings applied ALONGSIDE `node`'s:
 * - every quasi of its own template literal (an interpolation can split a
 *   dark: token away from its light twin into a separate TemplateElement); and
 * - every UNCONDITIONAL string argument of the enclosing class call. `cn` is
 *   twMerge(clsx(…)), so the last argument wins: a `dark:`-only argument is
 *   meaningless on its own and must be judged with its light sibling.
 *
 * A string behind `&&`/`?:`, or inside an object (cva's variants), deliberately
 * does NOT seed: it may not be applied at all, so its light token cannot stand
 * in for the dark one.
 */
function externalKeysFor(node, sourceCode) {
  const keys = new Set()
  if (node.type === 'TemplateElement' && node.parent && Array.isArray(node.parent.quasis)) {
    for (const q of node.parent.quasis) for (const k of lightKeysOf(q.value.cooked ?? '')) keys.add(k)
  }
  const call = enclosingClassCall(node, sourceCode)
  if (call) for (const arg of call.arguments) keysFromArgument(arg, keys)
  return keys
}

/** `rewriteClassString` for a specific class-owning node. */
function computeRewrite(node, text, sourceCode) {
  return rewriteClassString(text, externalKeysFor(node, sourceCode), {
    emptyAsSeparator: node.type === 'TemplateElement',
  })
}

/** Same quoting as the original: reuse the raw quote char (Literal) or replace only the raw chunk between delimiters (TemplateElement). */
function buildFixer(node, out) {
  if (node.type === 'TemplateElement') {
    // A TemplateElement's range spans the delimiters on both sides: 1 char
    // leading (the opening backtick, or `}` closing the prior expression)
    // and, trailing, 1 char if this is the tail quasi (closing backtick) or
    // 2 chars otherwise (the `${` opening the next expression). Verified
    // empirically against espree's actual ranges (not documented) and the
    // RuleTester template-literal case.
    return (fixer) => fixer.replaceTextRange([node.range[0] + 1, node.range[1] - (node.tail ? 1 : 2)], out)
  }
  return (fixer) => fixer.replaceText(node, node.raw[0] + out + node.raw[0])
}

/**
 * Does this node's class string put an accent or `-strong` fill under the text?
 * Checked against the REWRITTEN string (so `bg-blue-600` counts once the fixer
 * turns it into `bg-accent`) and against what the original tokens map to (so it
 * counts on the pass that has not been applied yet).
 */
function hasInkFill(text, out) {
  const candidates = []
  for (const raw of out.split(/\s+/).filter(Boolean)) {
    candidates.push(splitImportant(splitVariants(raw)[1])[1])
  }
  for (const raw of text.split(/\s+/).filter(Boolean)) {
    const to = MAPPING[splitImportant(splitVariants(raw)[1])[1]]
    if (to) candidates.push(to)
  }
  return candidates.some((b) => INK_FILL.test(b))
}

function check(context, node, text, sourceCode) {
  const { out, changed } = computeRewrite(node, text, sourceCode)
  // A dark: token the fixer left in place is a residual: its report carries no
  // fix. (The node-wide fix, when there is one, rides on the report of a token
  // that actually changed — every changed token is itself reported.)
  const survivors = new Set(out.split(/\s+/).filter(Boolean))
  const inkFill = hasInkFill(text, out)
  let fixUsed = false
  const takeFix = () => {
    if (!changed || fixUsed) return undefined
    fixUsed = true
    return buildFixer(node, out)
  }
  for (const raw of text.split(/\s+/)) {
    if (!raw) continue
    if (HEX.test(raw)) { context.report({ node, messageId: 'hex', data: { cls: raw }, fix: takeFix() }); continue }
    const variants = raw.split(':')
    const base = splitImportant(variants[variants.length - 1])[1]
    if (variants.slice(0, -1).includes('dark')) {
      context.report({ node, messageId: 'dark', data: { cls: raw }, fix: survivors.has(raw) ? undefined : takeFix() })
      continue
    }
    // Which ink token applies depends on the fill, so this one is never
    // auto-fixed — the plan's residual rules make it a per-site hand map.
    if (/^text-white(\/\d+)?$/.test(base) && inkFill) { context.report({ node, messageId: 'whiteOnFill', data: { cls: raw } }); continue }
    if (PALETTE.test(base)) context.report({ node, messageId: 'palette', data: { cls: raw }, fix: takeFix() })
    else if (Z_LITERAL.test(base)) context.report({ node, messageId: 'zLiteral', data: { cls: raw }, fix: takeFix() })
    else if (PX_LITERAL.test(base)) context.report({ node, messageId: 'pxLiteral', data: { cls: raw }, fix: takeFix() })
    // MAPPING is the single source of truth for which bare tokens are raw
    // palette even when they don't fit the graded-shade regexes above (e.g.
    // bg-white has no numeric shade suffix). A `null` value (e.g. text-white)
    // means "known, but not safe to flag automatically — case-by-case
    // hand-mapping only" per the plan's residuals rule, so it is excluded
    // here rather than turned into a sitewide error this task doesn't fix.
    else if (MAPPING[base]) context.report({ node, messageId: 'palette', data: { cls: raw }, fix: takeFix() })
  }
}

export default {
  meta: {
    type: 'problem',
    fixable: 'code',
    docs: { description: 'Use design tokens (bg-surface, text-ink-muted, z-dialog, min-h-target) instead of raw palette / dark: / hex / px literals' },
    messages: {
      palette: '"{{cls}}" is a raw palette class — use a token (bg-surface, text-ink-muted, border-edge, bg-success…).',
      dark: '"{{cls}}" uses dark: — tokens flip with the theme; drop the variant.',
      hex: '"{{cls}}" embeds a hex color — use a token.',
      zLiteral: '"{{cls}}" is a z-index literal — use z-raised|sticky|nav|popover|overlay|drawer|dialog|palette|toast.',
      pxLiteral: '"{{cls}}" is an arbitrary px size — use the scale (min-h-target, text-sm…).',
      whiteOnFill: '"{{cls}}" pairs text-white with an accent/-strong fill — use text-accent-ink (accent) or text-on-strong (-strong).',
    },
    schema: [],
  },
  create(context) {
    const sourceCode = context.sourceCode
    return {
      Literal(node) {
        if (typeof node.value === 'string' && inClassContext(node, sourceCode)) check(context, node, node.value, sourceCode)
      },
      TemplateElement(node) {
        if (inClassContext(node, sourceCode)) check(context, node, node.value.cooked ?? '', sourceCode)
      },
    }
  },
}

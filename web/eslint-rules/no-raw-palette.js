// ccswitch/no-raw-palette — bans Tailwind palette literals, dark: variants, hex
// colors, z-index literals and arbitrary pixel sizes inside class strings.
// Scope is decided in eslint.config.js (src/ui, src/shell now; wider per
// sub-spec C). Rationale: docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md §2.5.
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

const CLASS_CALLS = new Set(['cn', 'cva', 'clsx', 'twMerge'])

// The fixer table (§Mapping). Keys are the LIGHT class (any variant prefix
// such as hover:/focus-visible:/placeholder: is preserved on the output). A
// dark: token is deleted when the same string also has a light token that
// mapped; a dark: token with no light partner is mapped through this same
// table by its base, with the dark: prefix removed. `null` means "known, but
// not safe to auto-fix generically" — see the plan's hand-mapping rules.
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

/** The set of light-token bases in `text` that MAPPING would rewrite. */
function lightBasesOf(text) {
  const bases = new Set()
  for (const raw of text.split(/\s+/).filter(Boolean)) {
    const [variants, base] = splitVariants(raw)
    if (variants.split(':').includes('dark')) continue
    if (MAPPING[base]) bases.add(base)
  }
  return bases
}

/**
 * Pure codemod: rewrite a class string through MAPPING. `externalLightBases`
 * (optional) seeds the "does a light partner exist" check with light bases
 * found in SIBLING template-literal quasis — a `dark:` token split into its
 * own TemplateElement by an interpolation still needs to know its light twin
 * mapped elsewhere in the same literal. Interior whitespace collapses to a
 * single space; a leading/trailing whitespace run survives only if the
 * rewritten body is non-empty (an all-dropped chunk produces '', not ' ').
 */
export function rewriteClassString(text, externalLightBases) {
  const tokens = text.split(/\s+/).filter(Boolean)
  const out = []
  let changed = false
  const mappedLightBases = new Set(externalLightBases)
  // pass 1: light tokens
  for (const raw of tokens) {
    const [variants, base] = splitVariants(raw)
    if (variants.split(':').includes('dark')) continue
    const to = MAPPING[base]
    if (to) { out.push(variants + to); mappedLightBases.add(base); changed = true }
    else out.push(raw)
  }
  // pass 2: dark tokens — drop when a light token in the same string (or a
  // sibling quasi) mapped, else map by base
  for (const raw of tokens) {
    const [variants, base] = splitVariants(raw)
    const parts = variants.split(':').filter(Boolean)
    if (!parts.includes('dark')) continue
    const to = MAPPING[base]
    if (to && mappedLightBases.size > 0) { changed = true; continue }
    if (to) { out.push(parts.filter((p) => p !== 'dark').map((p) => p + ':').join('') + to); changed = true; continue }
    out.push(raw)
  }
  // dedupe while keeping order
  const seen = new Set()
  const deduped = out.filter((t) => (seen.has(t) ? false : (seen.add(t), true)))
  const leadingWs = /^\s/.test(text) ? ' ' : ''
  const trailingWs = /\s$/.test(text) ? ' ' : ''
  const body = deduped.join(' ')
  const outStr = body ? leadingWs + body + trailingWs : ''
  return { out: outStr, changed: changed || outStr !== text }
}

function inClassContext(node, sourceCode) {
  for (const a of sourceCode.getAncestors(node)) {
    if (a.type === 'JSXAttribute' && a.name && a.name.name === 'className') return true
    if (a.type === 'CallExpression' && a.callee.type === 'Identifier' && CLASS_CALLS.has(a.callee.name)) return true
  }
  return false
}

/**
 * `rewriteClassString` for a specific class-owning node. A `TemplateElement`
 * gets the light bases of every quasi in its enclosing `TemplateLiteral` as
 * external context (see the interior-whitespace/cross-quasi note above); a
 * `Literal` is a self-contained string, so it is called with no external
 * bases.
 */
function computeRewrite(node, text) {
  if (node.type === 'TemplateElement' && node.parent && Array.isArray(node.parent.quasis)) {
    const external = new Set()
    for (const q of node.parent.quasis) {
      for (const base of lightBasesOf(q.value.cooked ?? '')) external.add(base)
    }
    return rewriteClassString(text, external)
  }
  return rewriteClassString(text)
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

function check(context, node, text) {
  const { out, changed } = computeRewrite(node, text)
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
    const base = variants[variants.length - 1]
    if (variants.slice(0, -1).includes('dark')) { context.report({ node, messageId: 'dark', data: { cls: raw }, fix: takeFix() }); continue }
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
    },
    schema: [],
  },
  create(context) {
    const sourceCode = context.sourceCode
    return {
      Literal(node) {
        if (typeof node.value === 'string' && inClassContext(node, sourceCode)) check(context, node, node.value)
      },
      TemplateElement(node) {
        if (inClassContext(node, sourceCode)) check(context, node, node.value.cooked ?? '')
      },
    }
  },
}

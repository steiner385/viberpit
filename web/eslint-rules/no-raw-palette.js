// ccswitch/no-raw-palette — bans Tailwind palette literals, dark: variants, hex
// colors, z-index literals and arbitrary pixel sizes inside class strings.
// Scope is decided in eslint.config.js (src/ui, src/shell now; wider per
// sub-spec C). Rationale: docs/superpowers/specs/2026-09-02-ui-foundation-tokens-primitives-design.md §2.5.
const PALETTE =
  /^(bg|text|border|ring|fill|stroke|from|to|via|outline|decoration|divide|caret|shadow|placeholder)-(gray|slate|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}(\/\d+)?$/
const Z_LITERAL = /^z-(\[.*\]|\d+)$/
const PX_LITERAL = /^(text|min-h|min-w|max-h|max-w|h|w|p[xy]?|m[xy]?|gap)-\[\d+px\]$/
const HEX = /\[#[0-9a-fA-F]{3,8}\]/

const CLASS_CALLS = new Set(['cn', 'cva', 'clsx', 'twMerge'])

function inClassContext(node, sourceCode) {
  for (const a of sourceCode.getAncestors(node)) {
    if (a.type === 'JSXAttribute' && a.name && a.name.name === 'className') return true
    if (a.type === 'CallExpression' && a.callee.type === 'Identifier' && CLASS_CALLS.has(a.callee.name)) return true
  }
  return false
}

function check(context, node, text) {
  for (const raw of text.split(/\s+/)) {
    if (!raw) continue
    if (HEX.test(raw)) { context.report({ node, messageId: 'hex', data: { cls: raw } }); continue }
    const variants = raw.split(':')
    const base = variants[variants.length - 1]
    if (variants.slice(0, -1).includes('dark')) { context.report({ node, messageId: 'dark', data: { cls: raw } }); continue }
    if (PALETTE.test(base)) context.report({ node, messageId: 'palette', data: { cls: raw } })
    else if (Z_LITERAL.test(base)) context.report({ node, messageId: 'zLiteral', data: { cls: raw } })
    else if (PX_LITERAL.test(base)) context.report({ node, messageId: 'pxLiteral', data: { cls: raw } })
  }
}

export default {
  meta: {
    type: 'problem',
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

# Markdown component identity

- **Symptom:** inline diagrams repeatedly render and change transcript height;
  links become difficult to click as the virtual transcript measures itself.
- **Anti-pattern:** defining React component overrides inside a renderer creates
  new component types on every parent render. Stateful descendants unmount and
  restart their effects even when the Markdown source has not changed.
- **Fix shape:** memoize the component map against only its actual dependencies.
  Verify an unchanged-source parent rerender does not restart diagram rendering,
  and exercise the virtual transcript in a mobile browser.
- **Where to look:** `components` props on Markdown/rendering libraries. The
  repository sweep found one such map, in the shared `Markdown.tsx`; all its
  consumers now share the stable overrides.

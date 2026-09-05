# Settings / Preferences Screens — Industry Standards Reference

**Date:** 2026-09-04 · **Purpose:** evidence base for
`docs/superpowers/specs/2026-09-04-settings-redesign-design.md` · **Scope:** developer-facing web
and desktop apps · **Method:** web research against primary sources (design-system guidance, WCAG
2.2, product documentation). All sources verified 2026-09-04 unless noted. Items marked
**[synthesis]** are the author's inference, not a source's claim.

## 1. Information architecture

- **How many groups.** Microsoft's Windows app-settings guidance: "Try to keep the total number of
  settings to a maximum of four or five" (top-level sections), "Group similar or related settings
  under one section header," and "Combine less-used settings into a SettingsExpander so that common
  settings can each have their own SettingsCard." ([Microsoft, Guidelines for app
  settings](https://learn.microsoft.com/en-us/windows/apps/design/app-settings/guidelines-for-app-settings),
  page dated 2026-04-08). Apple: "Minimize the number of settings you offer" and "Aim to provide
  default settings that give the best experience to the largest number of people." ([Apple HIG,
  Settings](https://developer.apple.com/design/human-interface-guidelines/settings)).
- **What belongs in settings.** Apple: "Put general, infrequently changed settings in your custom
  settings area" and "prefer letting people modify task-specific options without going to your
  settings area." Microsoft: "Commands that are part of the typical app workflow ... shouldn't be in
  a settings page."
- **Naming.** Microsoft: "Use descriptive, one-word labels for settings headers. For example, name
  the setting 'Accounts' instead of 'Account settings'." Recommends a trailing **About** section
  (app name, icon, version, links). Theme options should read exactly "Light / Dark / Use system
  setting."
- **Product examples of top-level structure.** Linear's 2024 redesign has four sections: **Account**
  ("Personal settings, notifications, and preferences"), **Features**, **Administration** ("admins
  only"), **Your teams** ([Linear changelog
  2024-12-18](https://linear.app/changelog/2024-12-18-personalized-sidebar)); personal Preferences
  are subgrouped General / Interface and theme / Desktop application / Automations ([Linear
  docs](https://linear.app/docs/account-preferences)). Vercel splits **Team Settings** vs **Project
  Settings**, with project sections General, Build and Deployment, Domains, Environment Variables,
  Git, Integrations, Deployment Protection, Functions, Cron Jobs, Members, Webhooks, Drains,
  Security, Advanced ([Vercel, Project
  settings](https://vercel.com/docs/project-configuration/project-settings), updated 2026-09-01).
  GitHub account settings include Appearance, Accessibility, Notifications, Security and analysis,
  etc. ([GitHub Docs](https://docs.github.com/en/account-and-profile/how-tos/account-settings)).
- **Ordering.** No primary source mandates alphabetical. Microsoft says give "common settings" their
  own card and fold "less-used" into expanders, i.e. frequency-first **[synthesis]**. Apple (macOS):
  "Restore the most recently viewed pane."
- **Nest vs flatten.** Microsoft: "Avoid nesting expanders deeper than one level"; "Present content
  from top to bottom in a single column, scrollable if necessary." Apple: "iOS Settings uses a
  hierarchy of lists to help people choose options" and "A disclosure indicator reveals the next
  level in a hierarchy" ([Apple HIG, Lists and
  tables](https://developer.apple.com/design/human-interface-guidelines/lists-and-tables)).
- **Sidebar vs tabs vs single page.** Microsoft: in a NavigationView, "app settings should be the
  last item in the list of navigational choices and be pinned to the bottom," and the page "should
  open full-screen ... with a constrained max width (around 1000–1100 px)." Vercel, Linear, GitHub,
  VS Code all use a left sub-nav inside the settings area with one scrolling page per section
  **[observation from the sources above]**.
- **Search.** macOS System Settings: "use the search field—results appear in the sidebar as you
  type" ([Apple Support](https://support.apple.com/en-is/guide/mac-help/mchl8d10839d/mac)). VS Code's
  Settings editor "filters the settings to only show those that match your criteria," with
  `@modified`, `@ext`, `@feature`, `@lang`, `@tag:advanced` filters ([VS Code, User and workspace
  settings](https://code.visualstudio.com/docs/getstarted/settings)). No GitHub documentation of a
  dedicated settings search was found. Expectation **[synthesis]**: search becomes expected once
  settings exceed roughly one screen per section, and it should match on description text, not only
  titles.

## 2. Save semantics

- **Immediate apply for toggles.** Microsoft: "When a user changes a setting, the app should
  immediately reflect the change — don't require a confirmation button." Material 3: "The effects of
  a switch should start immediately, without needing to save." NN/g: "Toggle switches should take
  immediate effect and should not require the user to click Save or Submit" ([Material 3
  Switch](https://m3.material.io/components/switch/guidelines); [NN/g Toggle-Switch Guidelines,
  2018](https://www.nngroup.com/articles/toggle-switch-guidelines/)).
- **Explicit save for text/declarative inputs.** GitHub Primer: "Start with an explicit saving
  pattern" for forms; auto-save is for "imperative controls" (toggle switches, segmented controls,
  single-select dropdowns). Declarative controls (text inputs, checkboxes, radios, native selects)
  need explicit save because "Screenreader users can't read the options without selecting them,
  which could accidentally apply a selection the user didn't want." Critical rule: "never mix save
  patterns in a single form" and avoid mixing across forms on one page ([Primer,
  Saving](https://primer.style/product/ui-patterns/saving/)).
- **Expectation mismatch.** NN/g: "most users assume nothing is really changed until a Save or Apply
  button is pressed"; if you auto-save, show "Saved" beside each field or keep a Save button ([NN/g,
  Efficiency vs. Expectations, 2015](https://www.nngroup.com/articles/efficiency-vs-expectations/)).
  NN/g separately: "Separate controls that produce instant results from those that require clicking
  a command button."
- **Confirmation and errors.** Primer: on success use an inline message (or banner after redirect);
  "Avoid using a toast for now" (accessibility issues); on failure "the user's data should be
  preserved in the form and they should be given feedback about the failure." Unsaved-changes: use
  `beforeunload`, noting browsers no longer allow custom text.
- **Undo.** Material 3 snackbar: "To allow users to amend choices, display an 'Undo' action"; a
  snackbar "can contain a single action," auto-dismisses "after 4–10 seconds," and "Only one
  snackbar may be displayed at a time" ([Material 3
  Snackbar](https://m3.material.io/components/snackbar/guidelines)). NN/g heuristic #3: "Support undo
  and redo" ([NN/g, User Control and
  Freedom](https://www.nngroup.com/articles/user-control-and-freedom/)).
- **Per-field vs per-form.** No source mandates one; Primer's rule is consistency within a form.
  Practical pattern **[synthesis]**: one Save per card/section (Vercel style), immediate-apply for
  switches, never both in one card.

## 3. Control selection

- **Switch vs checkbox vs radio.** Material 3: "Use switches to select standalone or more verbose
  options in a list, like settings"; "Use checkboxes to select multiple related options in a list";
  "Use radio buttons to select a single option in a list"; "Avoid using a switch to select multiple
  options that require people to save." Apple (macOS): "Prefer a switch for settings that you want
  to emphasize"; "Use a checkbox instead of a switch if you need to present a hierarchy of
  settings." iOS: "Use the switch toggle style only in a list row." Microsoft: toggle for on/off;
  "radio buttons ... up to 5 mutually exclusive, related options"; combo box for compact choice
  lists. NN/g: radio buttons over dropdowns where space allows because "they make all options
  permanently visible" ([Apple HIG
  Toggles](https://developer.apple.com/design/human-interface-guidelines/toggles); [NN/g Checkboxes
  vs Radio Buttons](https://www.nngroup.com/articles/checkboxes-vs-radio-buttons/)).
- **Labels.** Material 3: "A label should describe what the control does when the switch is on."
  NN/g: labels "should not be neutral or ambiguous" (e.g., "Email notifications"). Apple: "Make sure
  the visual differences in a toggle's state are obvious" and don't rely on color alone.
- **Defaults.** NN/g: "always offer a default selection for radio button lists" in apps; GOV.UK's
  opposite rule ("Do not pre-select radio options") is for one-question-per-page forms, not
  persisted settings ([GOV.UK Radios](https://design-system.service.gov.uk/components/radios/)). For
  settings, a default is always present **[synthesis]**. VS Code shows a "colored bar on the left of
  the setting" when a value differs from default, with a gear menu to "reset a setting to its
  default value."
- **Disabled vs hidden.** Microsoft: "Display the same settings regardless of the app context. If
  some settings aren't relevant ... disable the SettingsCard" and "Add a descriptive message if one
  of the controls is disabled." Jakob Nielsen (2025-11-13): 76% of surveyed sources recommend a muted
  disabled state "supplemented by an explanation"; hide only features "never available to specific
  users" ([Nielsen, Inactive GUI Controls](https://jakobnielsenphd.substack.com/p/inactive-buttons)).
  Claude Code Desktop does this: a disabled Local option "stays in the environment dropdown but is
  grayed out ... with a tooltip saying your organization turned it off" ([Claude Code Desktop
  docs](https://code.claude.com/docs/en/desktop)).
- **Confirmation for toggles.** No design system requires it for ordinary toggles; WCAG 3.3.4
  requires reversible/checked/confirmed only when the action modifies or deletes user data (see §4).
  Use a confirm step for toggles with side effects such as permissions bypass **[synthesis]**; Claude
  Code Desktop pairs its bypass-permissions toggle with explicit warnings.
- **Help text.** Microsoft's SettingsCard has Header + Description + action control. GOV.UK: "Keep
  each hint to a single short sentence, without any full stops" and avoid links in hints ([GOV.UK
  Checkboxes](https://design-system.service.gov.uk/components/checkboxes/)). Tooltips are not a
  substitute for a visible description **[synthesis; consistent with WCAG 3.3.2]**.

## 4. Danger zone / destructive actions

- **GitHub pattern.** Repo Settings → General → "Danger Zone" → "Delete this repository" → "I want to
  delete this repository" → "I have read and understand these effects" → type the repo name →
  "Delete this repository"; warns "This action cannot be undone" ([GitHub
  Docs](https://docs.github.com/en/repositories/creating-and-managing-repositories/deleting-a-repository)).
- **Vercel pattern.** Delete Project section at "the bottom of the General page"; dialog requires
  "entering the project name and prompt." Pause Project (reversible) sits just above; Resume "does
  not ask for confirmation", i.e. friction scales with irreversibility
  ([Vercel](https://vercel.com/docs/projects/managing-projects)).
- **WCAG 3.3.4 (AA).** For pages "that modify or delete user-controllable data," at least one of
  Reversible, Checked, Confirmed must hold; sufficient techniques include "Requesting confirmation"
  and "Providing recovery of deleted information" ([WCAG 2.2 Understanding
  3.3.4](https://www.w3.org/WAI/WCAG22/Understanding/error-prevention-legal-financial-data.html)).
- **Dialog structure.** Polaris: modals for destructive confirmations, "no more than two buttons,"
  red destructive styling ([Polaris Common
  actions](https://polaris-react.shopify.com/patterns/common-actions/overview)). NN/g: distinguish
  Cancel from Close and use text labels, not an X.

## 5. Scope indication

- **Explicit scope labels.** VS Code exposes **User** vs **Workspace** tabs: user settings "apply
  globally to any instance of VS Code you open," workspace settings "override user settings";
  precedence Default → User → Remote → Workspace → Folder → Language → Policy. Claude Code: Managed →
  command line → Project local (`.claude/settings.local.json`, "You, this project") → Shared project
  → User; "Environment variables aren't a level in this stack ... decided per pair" ([Claude Code
  settings](https://code.claude.com/docs/en/settings)).
- **Device-local preferences.** Slack's notification preferences use separate **Desktop** and
  **Mobile** tabs with "mobile overrides" ([Slack
  Help](https://slack.com/help/articles/201355156-Configure-your-Slack-notifications)). Linear groups
  per-install options under a "Desktop application" heading. Claude Code Desktop stores site
  approvals "on your device" and the local env editor "encrypted on your machine."
- **Sync.** VS Code Settings Sync "share[s] settings, keyboard shortcuts, and installed extensions
  across your VS Code installs" but "does not synchronize your extensions to or from a remote
  window."
- **[synthesis]** Label browser-local preferences per group ("This browser only") rather than per
  control, and place them in a distinct section so users don't expect them on another device.

## 6. Responsive / mobile

- **Two-pane collapses to one.** Android list-detail: "In large windows, the list and detail panes
  appear side by side. In small windows, only one pane is visible at a time"; back from detail
  "return[s] to the list view, as this represents a clear layout change" ([Android
  list-detail](https://developer.android.com/develop/ui/compose/layouts/adaptive/list-detail)).
  Material 3 defines list-detail as "two side-by-side panes" configured per
  compact/medium/expanded breakpoint ([Material 3 canonical
  layouts](https://m3.material.io/foundations/layout/canonical-layouts/overview)).
- **iOS.** Settings is "a hierarchy of lists" with disclosure indicators and a back button in the
  navigation bar.
- **Deep links.** VS Code's gear menu can "copy the settings URL"; Windows uses `ms-settings:` URIs.
  **[synthesis]** Give every section (and ideally each setting) a stable route/anchor so status
  badges and docs can link to it, and so phone back-navigation returns to the section list.

## 7. Accessibility (WCAG 2.2 + ARIA)

- **1.3.5 Identify Input Purpose (AA):** purpose of fields "collecting information about the user"
  must be programmatically determinable; use `autocomplete` on name/email fields ([Understanding
  1.3.5](https://www.w3.org/WAI/WCAG22/Understanding/identify-input-purpose.html)).
- **2.4.11 Focus Not Obscured (Minimum) (AA):** a focused component "is not entirely hidden due to
  author-created content"; sticky save bars need `scroll-padding` or must dismiss ([Understanding
  2.4.11](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html)).
- **2.5.8 Target Size (Minimum) (AA):** "at least 24 by 24 CSS pixels" unless
  spacing/equivalent/inline/user-agent/essential exceptions apply ([Understanding
  2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)).
- **3.3.1 (A):** error "identified and ... described to the user in text." **3.3.2 (A):** "Labels or
  instructions are provided when content requires user input." **3.3.3 (AA):** suggestions for
  correction provided when known. **3.3.7 Redundant Entry (A):** previously entered info is
  "auto-populated, or available for the user to select"
  ([3.3.1](https://www.w3.org/WAI/WCAG22/Understanding/error-identification.html),
  [3.3.2](https://www.w3.org/WAI/WCAG22/Understanding/labels-or-instructions.html),
  [3.3.3](https://www.w3.org/WAI/WCAG22/Understanding/error-suggestion.html),
  [3.3.7](https://www.w3.org/WAI/WCAG22/Understanding/redundant-entry.html)).
- **4.1.3 Status Messages (AA):** "Saved" confirmations must be "programmatically determined through
  role or properties ... without receiving focus": `role="status"` / `aria-live="polite"`
  ([Understanding 4.1.3](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html)).
- **Switch ARIA:** `role="switch"` with `aria-checked`; Space toggles; "It is critical the label on a
  switch does not change when its state changes" ([WAI-ARIA APG
  Switch](https://www.w3.org/WAI/ARIA/apg/patterns/switch/)).
- **Grouping:** "Group checkboxes together in a `<fieldset>` with a `<legend>`" (GOV.UK); USWDS:
  "Group each set of thematically related controls in a fieldset element. Use the legend element to
  offer a label" ([USWDS Form](https://designsystem.digital.gov/components/form/)).
- **Error placement/wording:** error text after the label and hint, with a red border and a hidden
  "Error:" prefix; be specific, avoid "invalid"/"please" ([GOV.UK Error
  message](https://design-system.service.gov.uk/components/error-message/)).

## 8. Status / health integration

- **Dot vs count.** Material 3: "A small badge is a simple circle, used to indicate an unread
  notification"; "A large badge contains label text communicating item count"; use large badges "in
  a navigation rail," small ones in tight spaces; max "four [characters], including a +"; "hide the
  badge once the destination has been selected" ([Material 3
  Badges](https://m3.material.io/components/badges/guidelines)).
- **Health in settings.** Claude Code Desktop's Settings "shows the current status of each
  permission. If either is denied, click the badge to open the relevant System Settings pane."
  Vercel shows a "paused" icon in the team switcher plus a Resume banner on the settings page.
- **[synthesis]** Use a dot on the settings rail item for "needs attention," a count inside the
  Accounts section header, and a click-through that lands on the affected row.

## 9. Anti-patterns (each tied to a source's positive rule)

- **Sprawl:** violates Apple "Minimize the number of settings" / Microsoft "four or five" cap.
- **Duplicating OS settings:** Apple: "avoid including redundant versions of them in your custom
  settings area."
- **Hiding options contextually:** Microsoft: "Display the same settings regardless of the app
  context"; disable with a reason instead.
- **Mystery toggles:** Material 3: switches "should always be paired with an inline label"; NN/g:
  labels not "neutral or ambiguous."
- **Mixed save semantics:** Primer: "never mix save patterns in a single form."
- **Workflow commands in settings / admin mixed with preferences:** Microsoft: "Don't include
  commands that are part of the common app workflow"; Linear separates Account from Administration
  ("admins only").
- **Auto-save with no feedback:** NN/g: "don't leave your users in the dark."
- **"Advanced" as a dumping ground:** no source endorses it; Microsoft's SettingsExpander is for
  *related* sub-options of a specific setting, and Vercel's "Advanced" holds two named features.
  **[synthesis]** Prefer "Developer" with a defined charter (config file, env, diagnostics).
- **Settings only in modals:** violates deep-linkability (VS Code settings URLs) and Microsoft's
  full-page guidance **[synthesis]**.

## 10. Developer-tool specifics

- **Modified indicator + JSON view.** VS Code: colored bar for non-default values, `@modified`
  filter, gear menu to reset / copy setting ID / copy JSON pair / copy settings URL; edits also
  possible directly in `settings.json`; Object values "are merged," primitives/arrays "overridden."
- **Env-var-only / locked config.** n8n: `<AREA>_MANAGED_BY_ENV=true` "locks the matching UI
  controls" (read-only); if a setting "appears as read-only and you didn't expect it," check that
  variable ([n8n
  docs](https://docs.n8n.io/deploy/host-n8n/configure-n8n/manage-settings-using-environment-variables)).
  Claude Code: managed SSH connections "are shown as managed, so users can select them but cannot
  edit or delete them"; the Config tab "isn't a view of your settings.json contents."
- **Scopes.** VS Code User/Workspace/Folder; Claude Code User/Shared project/Project local/Managed
  with "which file supplied each key" not shown at startup (`/status` and `claude doctor` exist for
  diagnosis). Vercel: "Team Settings" vs "Project Settings," plus `vercel.json` overrides.
- **Restart-required.** VS Code: "Some changes to the settings are applied immediately, while others
  may require a restart." Claude Code "watches your settings files and reloads them ... without a
  restart" but "reads some keys only once, at session start" and lists them. **[synthesis]** Mark
  such rows inline ("Takes effect after restart") and offer a Restart action in the confirmation.

---

## Checklist (source tag in brackets)

1. Top-level settings sections capped at ~5, with an About/version section last. [Microsoft]
2. One-word section names ("Accounts," not "Account settings"); theme options read Light / Dark /
   Use system setting. [Microsoft]
3. Personal preferences separated from admin/workspace configuration. [Linear; Microsoft]
4. Each row = header + description + one control; sub-options at most one level deep. [Microsoft
   SettingsCard/Expander]
5. Search-within-settings that matches descriptions once sections exceed a screen. [macOS System
   Settings; VS Code]
6. Switches apply immediately; text fields and multi-selects use an explicit Save; never both in
   one form. [Material 3; Primer; NN/g]
7. Visible "Saved" / inline success message (not only a toast), announced via `role="status"`.
   [NN/g; Primer; WCAG 4.1.3]
8. Failed saves keep the user's input and show a specific error next to the field, with "Error:"
   prefix for AT. [Primer; GOV.UK; WCAG 3.3.1/3.3.3]
9. Unsaved-changes guard on navigation. [Primer]
10. Undo offered for reversible immediate changes (single-action snackbar, one at a time).
    [Material 3]
11. Switch labels state what "on" does and never change with state; `role="switch"` +
    `aria-checked`. [Material 3; NN/g; ARIA APG]
12. Radio groups always have a default; non-default values show a "modified" mark with
    reset-to-default. [NN/g; VS Code]
13. Unavailable options are disabled with a reason, not hidden (hide only if never available to
    that user). [Microsoft; Nielsen 2025]
14. Related controls grouped in `fieldset`/`legend`; hints are one short sentence. [GOV.UK; USWDS]
15. Destructive actions isolated at the bottom of General in a visually distinct zone; typed-name
    confirmation; reversibility stated. [GitHub; Vercel; WCAG 3.3.4]
16. Every setting labeled with scope (user / workspace / this browser / managed) and locked rows
    say who manages them. [VS Code; Claude Code; n8n]
17. Settings that need a restart or session reload say so inline. [VS Code; Claude Code]
18. Sidebar collapses to a list → detail drill-down on phones; back returns to the list; each
    section deep-linkable. [Android/Material list-detail; Apple HIG; VS Code settings URL]
19. Pointer targets ≥ 24×24 CSS px; sticky save bars never fully obscure focus. [WCAG 2.5.8;
    2.4.11]
20. "Needs attention" shown as a dot on the nav item and a count in-section; badge clears once the
    destination is selected. [Material 3 Badges]

**Gaps:** Apple HIG and Material 3 pages are JS-rendered; content was retrieved through a
text-rendering proxy of the official URLs. Stripe's dashboard settings IA is not documented
publicly in enough detail to cite. No GitHub settings-search feature was found in docs.

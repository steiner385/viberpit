# In-Product AI Assistants — Industry Norms, Expectations and Standards

**Date:** 2026-09-04
**Purpose:** The evidence base for `2026-09-04-ai-assistant-design.md`. Scope is assistants
embedded in developer tools and operational dashboards (GitHub Copilot Chat, Cursor/Windsurf,
Claude Code Desktop, Datadog Bits, Grafana Assistant, Sentry Seer, Slack/Notion/Linear AI), not
consumer chatbots. Each section separates **table stakes** (every serious product does it),
**differentiators** and **emerging**, and ends with how the ccswitch design responds. Sources are
pages fetched or surfaced in search on 2026-09-04; where a page returned no usable body that is
stated.

---

## 1. Interaction norms (UX)

### Table stakes

| Norm | Evidence | ccswitch design |
|---|---|---|
| **Side panel / drawer with a keyboard shortcut and Esc to dismiss** | VS Code Copilot Chat View `Ctrl+Alt+I`, Quick Chat, new session `Ctrl+N` ([VS Code cheat sheet](https://code.visualstudio.com/docs/copilot/reference/copilot-vscode-features)); Datadog Bits "Ask Bits", `Cmd/Ctrl+I`, side panel alongside product pages ([Bits Chat](https://docs.datadoghq.com/bits_ai/bits_chat/)); Grafana Assistant sparkle icon + Workspace ([Grafana](https://grafana.com/blog/how-to-use-ai-to-analyze-and-visualize-can-data-with-grafana-assistant/)). Typical rail width 400–520 px, message column ≤ 768 px ([Setproduct](https://www.setproduct.com/blog/ai-chat-interface-ui-design)). | `?drawer=assistant`, `Cmd+;`, push column ≥ 1280 px, sheet below; already built by sub-spec B. |
| **Streaming with a Stop control; partial output kept** | Setproduct: missing stop = pitfall #1; Vercel AI SDK `stop` must persist the partial message ([AI SDK](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams)). | Existing session WS streams `textDelta`; `Esc` = stop (D8); the driver keeps partial turns. |
| **Empty state with clickable suggested prompts** | NN/G: "present them as clickable buttons rather than text" ([NN/G](https://www.nngroup.com/articles/ai-chatbots-design-guidelines/)); Slack assistant pane opens with suggested prompts and a dedicated `assistant:write` scope ([Slack](https://docs.slack.dev/ai/developing-agents/)). | Route-keyed starter chips (§4.9 of the design). |
| **Context from where the user is; explicit context chips** | Copilot `#selection`/`@workspace` + "Add Context" ([GitHub](https://docs.github.com/en/copilot/reference/chat-cheat-sheet)); Bits and Grafana infer context from the current page/dashboard. | `{route, sessionId}` chip on every prompt; visible on the bubble. |
| **Persistent thread + explicit "New chat"** | Copilot History/`Ctrl+N`/`/fork`; Grafana Workspace history; `useChat` is stateless so persistence is the product's job ([Ably](https://ably.com/topic/ai-stack/vercel-why-ai-chat-history-disappears-between-sessions)). | One thread per identity in the CLI transcript; "New chat" archives and recreates. |
| **Answer first, short; no filler; don't autoscroll past the answer start** | NN/G "Less chat, more answer" ([NN/G](https://www.nngroup.com/articles/less-chat-more-answer/)). | System-prompt output policy; existing transcript scroll behaviour. |
| **Per-message copy / regenerate / feedback** | Copilot immersive mode: edit, retry (with model switch), thumbs, copy-as-Markdown ([GitHub changelog](https://github.blog/changelog/2025-02-25-refining-messages-and-reloading-responses-in-copilot-chat-is-now-in-public-preview/)); Salesforce trust patterns list thumbs + edits ([Salesforce](https://www.salesforce.com/news/stories/ai-trust-patterns/)). | Copy exists in `TranscriptView`; regenerate = re-send (composer history); thumbs deferred (non-goal v1) — **gap accepted**. |

### Differentiators

- **Visible tool calls / steps** — Anthropic: "prioritize transparency by explicitly showing the
  agent's planning steps" ([Building effective agents](https://www.anthropic.com/engineering/building-effective-agents));
  Cursor/Windsurf inline diffs and command output per message; Claude Code Desktop proposes every
  change and waits ([desktop quickstart](https://code.claude.com/docs/en/desktop-quickstart)).
  → ccswitch renders `toolCall`/`toolResult` rows already; the design adds an `APP_ACTION` tool
  kind with human titles ("Listed 14 sessions").
- **Live artifacts in chat** — Grafana renders real panels in the reply; Bits builds dashboards
  from chat. → Deferred; app links (`/sessions/<id>`) are the v1 equivalent.
- **Model name on each response and a model picker** (Setproduct pitfall list). → `turnModel`
  event already shows the model; composer model menu reused.
- **Message states beyond streaming** (queued, thinking, error-with-recovery, stopped). → All exist
  in the session reducer.

### Emerging

- Copilot "queue or steer" mid-turn ([Copilot July 2026](https://github.blog/changelog/2026-07-30-github-copilot-in-visual-studio-code-july-2026-releases/))
  → ccswitch already queues while a turn runs and has "Send now" (#1031).
- Voice input on mobile (Datadog); "GenUI" — the assistant generates interface rather than text
  ([NN/G GenUI](https://www.nngroup.com/videos/genui-ai-generated-interfaces/)). → Out of scope.

---

## 2. Agentic safety norms

### Table stakes

| Norm | Evidence | ccswitch design |
|---|---|---|
| **Read-only by default; confirm side effects** | OpenAI Agents SDK `needs_approval` interruptions ([OpenAI](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)); "sensitive or irreversible tasks… should involve human oversight" ([OpenAI guide](https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf)); Salesforce "mindful friction". | Read tools silent; mutation tools → existing permission card; destructive tools → card with no "Always allow". |
| **Permission tiers** | VS Code Default/Bypass/Assisted + `/yolo` behind a warning; Claude Code Desktop Plan/Manual/Accept edits/Auto; Sentry admins can remove Seer's "Create PR" button org-wide ([Seer](https://docs.sentry.io/product/ai-in-sentry/seer/)). | Assistant mode fixed to interactive; grant cache ("Always allow") per thread; feature flag + Settings toggle as the org-wide off switch. |
| **Inherit the caller's RBAC** | "Bits Chat uses your Datadog role… it can only access the resources you have permission to view or modify" ([Datadog](https://docs.datadoghq.com/bits_ai/bits_chat/)); Grafana enforces RBAC ([Grafana](https://grafana.com/products/cloud/ai-assistant/)). | Tools call the app's own API with the user's identity (assistant token bound to identity + session). |
| **MCP tool annotations drive approval; treat annotations from untrusted servers as untrusted** | `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`; "readOnlyHint from a trusted server might be auto-approved, while destructiveHint gets a confirmation step" ([MCP blog 2026-03-16](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)). | `PermissionRegistry` consults annotations **only** for the `ccswitch` server it authors; third-party MCP trust unchanged. |
| **OWASP LLM Top 10 2025**: LLM01 prompt injection, LLM05 improper output handling, LLM06 excessive agency, LLM07 system-prompt leakage, LLM10 unbounded consumption | [OWASP PDF](https://owasp.org/www-project-top-10-for-large-language-model-applications/assets/PDF/OWASP-Top-10-for-LLMs-v2025.pdf); scoped, time-limited agent identity so "each agent is distinguishable in logs, revocable independently" ([Gravitee](https://www.gravitee.io/blog/owasp-top-10-for-llm-applications-2025-a-practical-guide)). | No shell/file/web tools; untrusted-data framing; route allowlist; per-session token; rate limits; `max_turns`; audit `via`. |
| **MCP security best practices (2026-07-28)**: no token passthrough, per-client consent, log elevation events with correlation ids | [MCP security](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices). | Token minted for the assistant only, never forwarded; `X-CCSwitch-Assistant-Session` correlates audit rows. |
| **Audit every tool call; cost/rate caps** | Same sources; LLM10. | `assistant.tool_call` audit; token-bucket limits; idle reap. |

### Differentiators / emerging

- **Approval fatigue is measured and real.** Users approve ~93 % of prompts; Claude Code's auto
  mode classifier catches ~83 % of overeager actions ([How we contain Claude](https://www.anthropic.com/engineering/how-we-contain-claude));
  humans miss about a third of dangerous requests ([The Register, Aug 2026](https://www.theregister.com/ai-and-ml/2026/08/06/humans-in-the-loop-miss-a-third-of-dangerous-ai-coding-agent-requests/5284236));
  0.8 % of agent actions are irreversible and "requiring humans to approve every action will create
  friction without necessarily producing safety benefits" ([Measuring agent autonomy](https://www.anthropic.com/news/measuring-agent-autonomy)).
  → The design confirms only mutations, never reads, and lets "Always allow" cover repeated
  low-risk mutations while keeping destructive tools un-grantable.
- **Prompt-injection classifiers on tool output** (Anthropic). → Deferred; structural defences
  first. Tracked as a Phase 4 candidate if the eval set shows injection sensitivity.
- **Layered guardrails** (input, output, tool-argument, approval) ([OpenAI](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)).
  → Argument validation at the tool boundary (Pydantic), approval at the registry, output limits
  at the API adapter.

---

## 3. Architecture norms

### Table stakes

| Norm | Evidence | ccswitch design |
|---|---|---|
| **Typed schemas; few high-leverage tools; descriptions are onboarding docs; return semantic names not ids; paginate/truncate** | [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents). | ~20 tools, each a thin adapter over one endpoint; titles + ids returned; transcript tails truncated. |
| **MCP as the integration standard** | Grafana ships an MCP server; VS Code agent mode speaks MCP ([GitHub](https://github.blog/news-insights/product-news/github-copilot-agent-mode-activated/)). | In-process SDK MCP server named `ccswitch`; can later be exposed as a stdio server for external clients without changing tools. |
| **Server-side orchestration; browser only renders; resumable streams** | Vercel resume-streams pattern ([AI SDK](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams)). | Daemon owns the loop; WS ring replay + `sinceSrcSeq` catch-up already exist. |
| **Persist on finish/stop, never overwrite newer server state with older client snapshots** | Same; [DEV](https://dev.to/whoffagents/vercel-ai-sdk-usechat-in-production-streaming-errors-and-the-patterns-nobody-writes-about-4ecf). | CLI transcript is the store; the reducer already reconciles by `seq`. |
| **Context management: compaction + tool-result clearing** | Claude API compaction (`compact_20260112`, trigger 150k) and context editing ([Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)). | The CLI's own compaction applies (assistant is a CLI session); thread reset is one click. |
| **System prompt: explicit, literal, sectioned, explain why** | [Prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices). | One frozen prompt module; stable prefix for caching. |
| **Observability with OTel GenAI semantics; no prompt content by default** | [OpenTelemetry 2026](https://opentelemetry.io/blog/2026/genai-observability/); [Datadog](https://www.datadoghq.com/blog/llm-otel-semantic-convention/). | Counters/histograms per tool and turn; audit stores lengths, not text. |

### Differentiators / emerging

- Programmatic eval suites of real multi-tool tasks (Anthropic tools post) → Phase 4 eval set.
- Requested-vs-applied metrics (ccswitch already does this for model approvals) → reused for
  "cards denied" rate.
- MCP 2026-07-28 stateless handles + Client ID Metadata Documents → not needed in-process.

---

## 4. Accessibility, platform, latency, transparency

### Table stakes

| Norm | Evidence | ccswitch design |
|---|---|---|
| **`aria-live="polite"` / `role="log"` for arriving messages; assertive only for errors** (WCAG 4.1.3) | [W3C ARIA23](https://www.w3.org/WAI/WCAG21/Techniques/aria/ARIA23); ChatGPT announces streamed replies via live regions ([Athen list](https://mailman22.u.washington.edu/pipermail/athen-list/2024-October/020021.html)). | Transcript container `aria-live="polite"` + `aria-busy`; final text announced, not deltas. |
| **Focus management**: focus into the drawer on open, back to the trigger on close, Esc, `role="dialog"` | [Make Things Accessible](https://www.makethingsaccessible.com/guides/chatbots-and-web-accessibility-addressing-usability-issues-and-embracing-inclusive-design/); [Orange a11y](https://a11y-guidelines.orange.com/en/articles/chatbot/); W3C notes WCAG lacks chatbot-specific guidance ([W3C WAI](https://www.w3.org/WAI/pages/about/projects/wai-coop/paper107.html)). | `DrawerHost` already provides dialog/complementary roles and focus trap; axe runs with `region` on. |
| **Mobile**: actions visible without hover; resizable/fullscreen chat | NN/G guideline 8; Datadog/Slack mobile parity. | Sheet mode on phone; existing touch affordances. |
| **Latency**: TTFT dominates perception; ~500 ms noticeable, ~800 ms abandonment; streaming feels 40–60 % faster | [Spheron SLO guide](https://www.spheron.network/blog/llm-inference-slo-ttft-itl-latency-budget-guide-2026/); [TianPan](https://tianpan.co/blog/2026/04/16/streaming-ttft-latency-perception). | Immediate "thinking" state; tool rows stream as they happen; `effort=low`; targets in design §11. |
| **AI labelling**: EU AI Act Art. 50 applies from 2 Aug 2026 — disclose "from the start of the first interaction in a clear and distinguishable manner" | [EC FAQ](https://digital-strategy.ec.europa.eu/en/faqs/transparency-obligations-under-article-50-ai-act); [CSA note](https://labs.cloudsecurityalliance.org/research/csa-research-note-eu-ai-act-article-50-transparency-20260729/). Light obligation for an internal ops tool, but a persistent badge costs nothing. | Persistent footer "AI-generated. Actions ask for confirmation." |

---

## 5. Published design guidelines (mapping)

| Guideline set | What it prescribes | Where ccswitch meets it |
|---|---|---|
| **Microsoft HAX** ([library](https://www.microsoft.com/en-us/haxtoolkit/library/)) | G1/G2 make clear what it can do and how well; G7/G8 efficient invocation/dismissal; G9 efficient correction; G11 why it did what it did; G12 remember recent interactions; G15 granular feedback; G16 convey consequences; G17 global controls; G18 notify about changes | starter chips + disclosure (G1/G2); `Cmd+;`/Esc (G7/G8); edit-and-resend (G9); tool rows (G11); persistent thread (G12); permission cards naming the consequence (G16); Settings toggle + kill switch (G17). G15 (thumbs) deferred. |
| **Google PAIR Guidebook v2** ([PAIR](https://pair.withgoogle.com/guidebook-v2/)) | Explain the system; calibrate trust; control vs automation; when something goes wrong | tool visibility; confirm-before-mutate; error states with retry. |
| **Apple HIG — Generative AI** ([Apple](https://developer.apple.com/design/human-interface-guidelines/generative-ai); page body did not fetch, secondary coverage only) | transparency; output is a suggestion requiring review; edit/regenerate/dismiss | disclosure; cards; composer history. |
| **IBM Carbon for AI** ([Carbon](https://carbondesignsystem.com/guidelines/carbon-for-ai/)) | AI label on every AI component with an explainability popover; open-source Carbon AI Chat | footer label; tool rows as the explanation. |
| **Salesforce trust patterns** ([Salesforce](https://www.salesforce.com/news/stories/ai-trust-patterns/)) | mindful friction; awareness of AI; explainability/citations; hallucination reduction | cards; label; app links back to the entity. |
| **NN/G** (10 guidelines, prompt controls, less-chat-more-answer) | clickable suggestions; standard labelled icons; answer-first | chips; existing composer iconography; prompt policy. |
| **Shopify Polaris AI** | no published AI pattern guidance surfaced (only a generic toolkit repo) — treated as not found. | — |
| **Slack** ([Slack dev docs](https://docs.slack.dev/ai/developing-agents/)) | suggested prompts, thread titles, scoped permission | chips; thread named "Assistant". |

---

## 6. Condensed checklist and gap register

**Table stakes met by the design:** drawer + shortcut + Esc; empty-state chips; streaming with stop
and partial retention; persistent thread + New chat; context chip; AI badge; polite live region +
focus management; read-only auto-run, mutation cards, destructive never-auto; caller's identity
inherited; every tool call audited; rate/turn caps; server-side loop over the existing WS with
resumable replay; visible tool rows; model name per turn.

**Accepted gaps (v1):**

| Gap | Why deferred | Revisit trigger |
|---|---|---|
| Thumbs up/down per message | needs a feedback store and a consumer; audit already captures denials | Phase 4 eval shows quality drift the team cannot see |
| `@` entity mentions in the composer | route/session context covers the dominant case; `search_sessions` covers the rest | users paste session ids by hand in >10 % of prompts |
| Live artifacts (session card in chat) | app links reach the same entity in one click | drawer usage on phone is high |
| Injection classifier on tool output | structural defences first | eval set adds injection cases and they fail |
| Multi-thread history | one thread per identity; audit keeps the rest | a second operator joins the deployment |

#!/usr/bin/env python3
"""Weighted parity benchmark: ViberPit web UI vs Claude Desktop 'Code' mode.

Re-run after each UI change and paste the output into
docs/audits/2026-11-01-claude-desktop-code-benchmark.md (section 3).
"""
# Benchmark: ViberPit vs Claude Desktop "Code" mode. Desktop = reference (1.0 per row).
# status: Y=1, P=0.5, N=0 ; weight 3=core loop, 2=important, 1=nice

S={"Y":1.0,"P":0.5,"N":0.0}
D=[
("1 Shell & IA",[
 ("Session sidebar + search",3,"Y","Active/Background/Archived scopes, server FTS"),
 ("Group/sort by project, folder, branch, status, env, date, PR",2,"Y","repo, status, branch, host (env), date, PR; folder = repo"),
 ("Pin / star sessions",1,"N",""),
 ("Read / unread marking",1,"Y","per-viewer bold + card menu, live and archived; no ⌘⌥U chord"),
 ("⌘K palette over sessions + branches",2,"Y","sessions, actions, nav, settings, repos, branches; repo/branch drill down inside the palette; each branch a session has on a repo offers \"New session on repo@branch\", opening the dispatch bar on it (D-19; nothing created until sent; no checkout, not a Desktop feature); keyboard-only on phone"),
 ("Split view / tiled panes",2,"N","umbrella spec non-goal"),
 ("Pop session into own window",1,"P","every session is a URL; no layout restore"),
 ("Archived view + unarchive",2,"Y",""),
 ("Session rail: Changes / Context / Environment / Tasks cards",2,"Y",""),
]),
("2 Transcript",[
 ("Streaming markdown + code highlight",3,"Y","GFM, KaTeX, Mermaid"),
 ("Collapsible tool-call groups",3,"Y",""),
 ("View modes Normal / Verbose / Thinking (Ctrl+O)",2,"Y","per session; Normal = Settings defaults"),
 ("Thinking blocks",2,"Y",""),
 ("Todo list rendering",2,"Y",""),
 ("Subagent reports + drill-down",2,"Y","VP has dedicated sub-agent page"),
 ("Compaction marker",1,"Y",""),
 ("Per-message copy as Markdown",2,"Y",""),
 ("Clickable file paths (open / attach as context)",2,"Y",""),
 ("In-transcript search",1,"Y","⌘F: server search over the replay, highlights, newest-first next/prev that pages back to any hit (D-11)"),
 ("Post-turn summary card (completed / ready for review)",1,"Y","transcript tail card: outcome, changed files with +/−, Open Changes, Likely complete (D-11)"),
]),
("3 Composer",[
 ("Enter/Shift+Enter, per-session drafts",3,"Y",""),
 ("Stop button + Esc stops the turn",3,"Y",""),
 ("Queue while busy + Send now (steer)",3,"Y","no reorder of queue"),
 ("Attachments: paste / drag / picker",2,"Y",""),
 ("@-file mentions",3,"Y",""),
 ("Slash-command palette",3,"Y","catalog pushed per session"),
 ("Model picker in prompt row",2,"Y",""),
 ("Effort picker in session",2,"Y","Codex: the catalog's levels; Claude: the current model's supportedEffortLevels, live mid-session through apply_flag_settings (D-14, #3269)"),
 ("Permission-mode picker (5 modes) in session",3,"Y",""),
 ("Fast-mode toggle",1,"Y","toggle + cost note; state shown is the stored choice (#3254)"),
 ("Prompt history (↑/↓ + Ctrl+R search)",1,"P","↑/↓ only"),
 ("Suggested next prompt (Tab)",1,"Y",""),
 ("Dictation",1,"Y","server STT + Web Speech"),
 ("Usage ring in prompt row",1,"Y","context only; plan usage → D-6"),
]),
("4 Commands & config",[
 ("Engine slash-command passthrough",3,"Y",""),
 ("App-handled /fork /rewind /btw /plan /effort /fast",2,"Y","/help /context /clear /model /account /mcp /fork /rewind /btw (opens the side chat and sends; D-12) /effort /fast (D-14) /plan /output-style (D-16a, human senders only); /add-dir out of scope (not in D15)"),
 ("Plugins + marketplace browse/install",2,"Y","curated marketplaces + reviewed Directory; consent card; every host (D-16c)"),
 ("Skills management",1,"Y",""),
 ("MCP servers + per-tool toggles",2,"Y","servers, OAuth, scope; per-tool switches: Settings default + live per-session override (D-16b)"),
 ("CLAUDE.md / memory-file editor",2,"Y","as decided (D8): the Context card lists every memory file the CLI loaded, with tokens, each viewable; Settings › Memory edits the user CLAUDE.md shared by every account (etag conflict, Keep my draft); repo files read-only. Claude only (Codex reports none); type labels are the CLI's (rules/imports show as User/Project); rocky sees a save after up to 10 min (#3246) (D-13)"),
 ("Hooks editor",1,"Y","Desktop has none — VP ahead"),
 ("Output styles",1,"Y","per-session picker in the + menu, Settings default and list; user styles shared across accounts; custom-style editing not yet"),
 ("Custom commands",1,"Y",""),
]),
("5 Settings",[
 ("PR behaviour: auto-fix, auto-archive on merge, branch prefix",1,"Y","per-session PR auto-fix switch in the Environment card (D-14) + auto-archive + autopilot; branch prefix editable in Settings › Sessions (D-22), deletion gated on recorded ownership"),
 ("Worktree location / policy",1,"Y","repo/worktree root (D-14) and branch prefix (D-22) editable in Settings › Sessions; automatic branch deletion requires a recorded-ownership row, so no prefix can make your own branches deletable"),
 ("Default transcript view",1,"Y",""),
 ("Notification levels per event type",1,"P","quiet hours, channels, routing rules"),
 ("Appearance: theme, font + transcript size",1,"P","theme + widths"),
 ("Presets / environments selectable at new session",2,"Y","dispatch bar + menu (removable chip) and ⌘K \"New session from preset\"; no chord — every digit family is taken (D-14)"),
 ("Sandbox settings",1,"N","built and shipped dark (D-23): opt-in per session, fail-closed, GitHub-only network, worktree-only writes, home unreadable, non-Bash tools confined; off until host blockers are fixed — AppArmor bwrap-userns-restrict breaks the CLI seccomp helper and the CLI proxy socket sits under the hidden home (#3367)"),
]),
("6 User feedback",[
 ("Permission card: once / this session / always / deny",3,"Y",""),
 ("AskUserQuestion card",3,"Y","multi-question batching"),
 ("Plan approval with comments",2,"Y","overall + per-step"),
 ("Per-session status (working / waiting / review-ready)",3,"Y",""),
 ("OS / push notifications with actions",2,"Y","Web Push"),
 ("Context meter + 'auto-compacts soon'",2,"Y",""),
 ("Plan-usage meter",2,"Y","per-window quota"),
 ("Error recovery actions (switch model, lower effort, rewind & retry)",2,"Y","switch model, lower effort (step down, or picker when unknown), rewind & retry; actions by error kind (D-19)"),
]),
("7 Lifecycle & branching",[
 ("New session pre-flight (folder/env/model/effort)",3,"Y","plus account + host"),
 ("Worktree visible + controls (branch, enter, discard)",2,"Y","branch + path; discard with recovery refs; repair (D-19)"),
 ("Resume / continue",3,"Y",""),
 ("Import CLI sessions",1,"Y","Adopt"),
 ("Fork session (from here)",3,"Y","Claude; files start from current worktree"),
 ("Rewind to message (with file restore, undo)",3,"Y","Claude; Edit/Write only — shell changes not restored"),
 ("Edit & resend a previous message",2,"Y",""),
 ("Side chat scoped to session (/btw, ⌘;)",2,"Y","Assistant drawer about the open session (D-12): ⌘; opens, /btw <q> opens and sends; reads its transcript/diff/search and is refused any write to it; a separate assistant thread reading through tools, not a fork of the session's own context"),
 ("Spin off parallel / child sessions",1,"Y",""),
 ("Archive / unarchive / auto-archive",2,"Y",""),
 ("Rename + AI titles",1,"Y",""),
 ("Move session to cloud / other host",1,"Y","other fleet host, idle Claude sessions (D-17); the self-hosted fleet stands in for Desktop's cloud (owner, 2026-11-06, D22c); moving Codex sessions tracked in #3347"),
 ("Scheduled tasks / routines",1,"P","Delivery automations"),
]),
("8 Code review & dev",[
 ("Changes pane: aggregate diff, unified/split, line comments",3,"Y",""),
 ("Inline tool diffs",2,"Y","shared DiffRenderer, highlighted; MultiEdit edits[] not rendered"),
 # Split in D-20: the owner rejected editing (D1), so it is its own rejected half-row and the
 # adopted half (view + content search) can be scored on its own. Total weight is unchanged.
 ("Files pane: view + content search",0.5,"Y","read-only viewer with line numbers + highlighting; content search over the worktree (D-20): git grep on the owning daemon, fixed/regex, results grouped by file, click opens the viewer at the line"),
 ("Files pane: edit",0.5,"N","editing rejected (D1): the Files pane stays read-only; edits go through the agent"),
 ("PR viewer: checks, threads, merge, auto-merge",2,"Y","PR tab (checks, review threads, merge / auto-merge; merge queue read-only)"),
 ("Preview browser with dev-server detection",2,"Y","detection + per-viewer open (local link / tailnet IP / VNC on demand); no proxy, no element select (D-18)"),
 ("Integrated terminal",2,"N",""),
]),
("9 Shortcuts & a11y",[
 ("Shortcut registry + ⌘/ sheet",2,"Y",""),
 ("Keyboard menus for mode / model / effort",1,"Y",""),
 ("Session switching by keyboard",2,"Y","⌘⌥[ ] and ⌘⌥1–9 in list order (Alt+[ ] / Alt+Shift+1–9 elsewhere)"),
 ("Jump between prompts",1,"Y","⌘⌥↑/↓ (Alt+↑/↓ elsewhere), as Desktop"),
 ("Accessibility (landmarks, live regions, reduced motion)",2,"Y","VP ahead"),
]),
("10 Remote & multi-device",[
 ("Use from phone",3,"Y","native PWA — VP ahead"),
 ("Push to phone",2,"Y",""),
 ("Remote hosts (SSH / fleet)",2,"Y","fleet runners"),
 ("Hand work over from phone (Dispatch)",1,"Y","dispatch bar on phone"),
]),
]
# Rows the owner rejected for the target (docs/audits/2026-11-01-claude-desktop-code-benchmark.md §7).
# They still count against parity but are excluded from "target completion".
REJECTED={
 "Split view / tiled panes","Integrated terminal","Pin / star sessions","Pop session into own window",
 "Prompt history (↑/↓ + Ctrl+R search)","Notification levels per event type",
 "Appearance: theme, font + transcript size","Scheduled tasks / routines",
 "Files pane: edit",
}
out=[]; tot=totw=0
summ=[]
for dim,rows in D:
    s=sum(w*S[st] for _,w,st,_ in rows); W=sum(w for _,w,_,_ in rows)
    tot+=s; totw+=W; summ.append((dim,s,W))
md=["| Dimension | ViberPit score | Parity |","|---|---:|---:|"]
for dim,s,W in summ: md.append(f"| {dim} | {s:g} / {W:g} | {100*s/W:.0f}% |")
md.append(f"| **Overall (weighted)** | **{tot:g} / {totw:g}** | **{100*tot/totw:.0f}%** |")
rows_t=[(w,st) for _,rows in D for f,w,st,_ in rows if f not in REJECTED]
ts=sum(w*S[st] for w,st in rows_t); tw=sum(w for w,_ in rows_t)
md.append(f"| **Target completion** (excl. rejected rows) | **{ts:g} / {tw:g}** | **{100*ts/tw:.0f}%** |")
md.append("")
for dim,rows in D:
    md+= [f"### {dim}","","| Desktop capability | Weight | ViberPit | Notes |","|---|:-:|:-:|---|"]
    for f,w,st,n in rows: md.append(f"| {f} | {w} | {dict(Y='✅',P='◐',N='❌')[st]} | {n}{' · *rejected for target*' if f in REJECTED else ''} |")
    md.append("")
gaps=sorted([(w*(1-S[st]),w,f,dim,st) for dim,rows in D for f,w,st,n in rows if st!="Y"],key=lambda x:(-x[0],x[3]))
md+=["### Ranked gaps (weight × missing)","","| # | Gap | Dimension | Points lost |","|---|---|---|---:|"]
for i,(g,w,f,dim,st) in enumerate(gaps,1): md.append(f"| {i} | {f} | {dim} | {g:g} |")
print("\n".join(md))

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
 ("Group/sort by project, folder, branch, status, env, date, PR",2,"P","group none/repo/status only"),
 ("Pin / star sessions",1,"N",""),
 ("Read / unread marking",1,"N","needs-you badge only"),
 ("⌘K palette over sessions + branches",2,"N",""),
 ("Split view / tiled panes",2,"N","umbrella spec non-goal"),
 ("Pop session into own window",1,"P","every session is a URL; no layout restore"),
 ("Archived view + unarchive",2,"Y",""),
 ("Session rail: Changes / Context / Environment / Tasks cards",2,"P","meta strip has ctx, host, bg tasks; no Changes"),
]),
("2 Transcript",[
 ("Streaming markdown + code highlight",3,"Y","GFM, KaTeX, Mermaid"),
 ("Collapsible tool-call groups",3,"Y",""),
 ("View modes Normal / Verbose / Thinking (Ctrl+O)",2,"P","Settings default only"),
 ("Thinking blocks",2,"Y",""),
 ("Todo list rendering",2,"Y",""),
 ("Subagent reports + drill-down",2,"Y","VP has dedicated sub-agent page"),
 ("Compaction marker",1,"Y",""),
 ("Per-message copy as Markdown",2,"N",""),
 ("Clickable file paths (open / attach as context)",2,"P","click-to-copy only"),
 ("In-transcript search",1,"N",""),
 ("Post-turn summary card (completed / ready for review)",1,"P","'Likely complete' panel"),
]),
("3 Composer",[
 ("Enter/Shift+Enter, per-session drafts",3,"Y",""),
 ("Stop button + Esc stops the turn",3,"P","Esc detaches instead of stopping"),
 ("Queue while busy + Send now (steer)",3,"Y","no reorder of queue"),
 ("Attachments: paste / drag / picker",2,"Y",""),
 ("@-file mentions",3,"N","@ matches session titles, assistant only"),
 ("Slash-command palette",3,"Y","catalog pushed per session"),
 ("Model picker in prompt row",2,"P","in meta strip"),
 ("Effort picker in session",2,"P","dispatch-time only"),
 ("Permission-mode picker (5 modes) in session",3,"P","Plan toggle only; Settings for the rest"),
 ("Fast-mode toggle",1,"N",""),
 ("Prompt history (↑/↓ + Ctrl+R search)",1,"P","↑/↓ only"),
 ("Suggested next prompt (Tab)",1,"Y",""),
 ("Dictation",1,"Y","server STT + Web Speech"),
 ("Usage ring in prompt row",1,"N","UsageRing built, unused"),
]),
("4 Commands & config",[
 ("Engine slash-command passthrough",3,"Y",""),
 ("App-handled /fork /rewind /btw /plan /effort /fast /add-dir",2,"P","/help /context /clear /model /account /mcp"),
 ("Plugins + marketplace browse/install",2,"P","enable/disable + detail; no marketplace"),
 ("Skills management",1,"Y",""),
 ("MCP servers + per-tool toggles",2,"P","servers, OAuth, scope; no per-tool toggles"),
 ("CLAUDE.md / memory-file editor",2,"N",""),
 ("Hooks editor",1,"Y","Desktop has none — VP ahead"),
 ("Output styles",1,"N",""),
 ("Custom commands",1,"Y",""),
]),
("5 Settings",[
 ("PR behaviour: auto-fix, auto-archive on merge, branch prefix",1,"P","auto-archive + autopilot; no branch prefix"),
 ("Worktree location / policy",1,"N","server-only"),
 ("Default transcript view",1,"Y",""),
 ("Notification levels per event type",1,"P","quiet hours, channels, routing rules"),
 ("Appearance: theme, font + transcript size",1,"P","theme + widths"),
 ("Presets / environments selectable at new session",2,"P","editable in Settings, not selectable at dispatch"),
 ("Sandbox settings",1,"N",""),
]),
("6 User feedback",[
 ("Permission card: once / this session / always / deny",3,"P","no 'this session' scope"),
 ("AskUserQuestion card",3,"Y","multi-question batching"),
 ("Plan approval with comments",2,"P","Approve & run / Keep planning"),
 ("Per-session status (working / waiting / review-ready)",3,"Y",""),
 ("OS / push notifications with actions",2,"Y","Web Push"),
 ("Context meter + 'auto-compacts soon'",2,"P","% only"),
 ("Plan-usage meter",2,"Y","per-window quota"),
 ("Error recovery actions (switch model, lower effort, rewind & retry)",2,"P","auto-resend, rotate"),
]),
("7 Lifecycle & branching",[
 ("New session pre-flight (folder/env/model/effort)",3,"Y","plus account + host"),
 ("Worktree visible + controls (branch, enter, discard)",2,"P","automatic but invisible"),
 ("Resume / continue",3,"Y",""),
 ("Import CLI sessions",1,"Y","Adopt"),
 ("Fork session (from here)",3,"N","engine capability exists, no UI"),
 ("Rewind to message (with file restore, undo)",3,"N",""),
 ("Edit & resend a previous message",2,"N",""),
 ("Side chat scoped to session (/btw, ⌘;)",2,"P","app-level assistant drawer"),
 ("Spin off parallel / child sessions",1,"Y",""),
 ("Archive / unarchive / auto-archive",2,"Y",""),
 ("Rename + AI titles",1,"Y",""),
 ("Move session to cloud / other host",1,"N",""),
 ("Scheduled tasks / routines",1,"P","Delivery automations"),
]),
("8 Code review & dev",[
 ("Changes pane: aggregate diff, unified/split, line comments",3,"N",""),
 ("Inline tool diffs",2,"P","rebuilt from tool input, no highlighting"),
 ("Files pane (view / edit)",1,"N",""),
 ("PR viewer: checks, threads, merge, auto-merge",2,"P","Pipeline page + PR pills + Fix"),
 ("Preview browser with dev-server detection",2,"P","VNC Browser tab, no dev-server detect"),
 ("Integrated terminal",2,"N",""),
]),
("9 Shortcuts & a11y",[
 ("Shortcut registry + ⌘/ sheet",2,"Y",""),
 ("Keyboard menus for mode / model / effort",1,"N",""),
 ("Session switching by keyboard",2,"P","list arrows; no ⌘1-9 / Ctrl+Tab"),
 ("Jump between prompts",1,"N",""),
 ("Accessibility (landmarks, live regions, reduced motion)",2,"Y","VP ahead"),
]),
("10 Remote & multi-device",[
 ("Use from phone",3,"Y","native PWA — VP ahead"),
 ("Push to phone",2,"Y",""),
 ("Remote hosts (SSH / fleet)",2,"Y","fleet runners"),
 ("Hand work over from phone (Dispatch)",1,"Y","dispatch bar on phone"),
]),
]
out=[]; tot=totw=0
summ=[]
for dim,rows in D:
    s=sum(w*S[st] for _,w,st,_ in rows); W=sum(w for _,w,_,_ in rows)
    tot+=s; totw+=W; summ.append((dim,s,W))
md=["| Dimension | ViberPit score | Parity |","|---|---:|---:|"]
for dim,s,W in summ: md.append(f"| {dim} | {s:g} / {W} | {100*s/W:.0f}% |")
md.append(f"| **Overall (weighted)** | **{tot:g} / {totw}** | **{100*tot/totw:.0f}%** |")
md.append("")
for dim,rows in D:
    md+= [f"### {dim}","","| Desktop capability | Weight | ViberPit | Notes |","|---|:-:|:-:|---|"]
    for f,w,st,n in rows: md.append(f"| {f} | {w} | {dict(Y='✅',P='◐',N='❌')[st]} | {n} |")
    md.append("")
gaps=sorted([(w*(1-S[st]),w,f,dim,st) for dim,rows in D for f,w,st,n in rows if st!="Y"],key=lambda x:(-x[0],x[3]))
md+=["### Ranked gaps (weight × missing)","","| # | Gap | Dimension | Points lost |","|---|---|---|---:|"]
for i,(g,w,f,dim,st) in enumerate(gaps,1): md.append(f"| {i} | {f} | {dim} | {g:g} |")
print("\n".join(md))

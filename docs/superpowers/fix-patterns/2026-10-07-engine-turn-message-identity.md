# Engine turn identity is not display message identity

**Symptom:** A follow-up prompt stays below subsequent replies, or repeated follow-ups disappear after replay (#2737).

**Anti-pattern:** Using an engine's turn ID as both the user-message identity and the sole assistant display group. One Codex turn can contain multiple user messages and responses after steering.

**Fix shape:** Give each user message its own identity (native item ID for replay; source UUID for live echoes). Preserve existing source UUIDs when reading older saved/live events. Continue assistant output in a new display segment after each confirmed prompt, retaining the native turn ID for routing. Keep the cursor ordinal on the original segment, and route re-supplied tools/questions/permissions by block identity so they update the original segment. A queued or unacknowledged prompt does not establish a response boundary.

**Where to look:** Engine event translators and live echoes; transcript reducer lookups; main and subagent transcript paths; replay and overlapping pagination; duplicate tool/question delivery. Audit found both Codex live steering and native replay reused turn IDs for user messages; both are corrected, and main/subagent output shares one segment-routing helper.

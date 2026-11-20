"""Verbatim texts of the spec: reminders (§5 (2)) and the rules-file template (§12.9)."""

_NUDGE_AFTER_SEARCH = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you generate. If a persistent N3MC memory (`n3mc-pro`, "
    "`n3mc-pg` or `n3mc-longtermmemory`) is also connected, route "
    "long-lived artifacts there; this server is for "
    "transient task scratch (7-day TTL). Do not skip the save just "
    "because no memory hit was returned above._"
)
_NUDGE_AFTER_SAVE = (
    "\n\n---\n_Reminder: if this turn produced multiple distinct facts or a "
    "long verbatim artifact, make sure each was saved — one "
    "`save_memory` call per fact, or one full-text call for verbatim "
    "long content (>400 chars). Exact duplicates are auto-rejected, so "
    "err on the side of saving._"
)
_NUDGE_GENERIC = (
    "\n\n---\n_Reminder: before closing this turn, call `save_memory` "
    "to persist the user's intent (paraphrased) and any substantive "
    "output you produced. Lite entries expire 7 days after save, so "
    "saving more is safer than saving less._"
)

_RULES_MD_TEMPLATE = '<!-- n3mc-lite-behavior v1 -->\n# N3MemoryCore Lite Behavioral Guidelines\n\nThese guidelines apply to every Claude Code session in this project. They are\nloaded automatically from `.claude/rules/` at session start. N3MC Lite is a\nworking memory: every record is deleted 7 days after it was saved.\n\n## Fully Automatic Saving\nAll conversations are saved by hooks (UserPromptSubmit + Stop). Do NOT call\n`--buffer` for them. Every non-empty user message and Claude response is\nsaved (fenced code blocks as `[code omitted]`); there is no length filter and\nno skip-pattern filter. Make NO acknowledgement when a save succeeds —\nsilence is correct. Run `{cli} --buffer "<text>"` only when the user asks\nyou to remember something specific.\n\n## Recalled Memory Context\nEvery turn, the UserPromptSubmit hook adds a `# Recalled Memory Context` block\nto your context. Each hit starts with `### ` and shows its `saved=` time and,\nwhen it has one, `turn=<turn_id>`; a `[doc×N]` hit is a long text shown in\nfull. When several versions of the same content appear, the one with the\nnewest `saved` time is the current one. If the block contains a\n`> ⚠️ N3MC notice:` line, tell the user what it says.\n\n## Weak Matches\nRecords under `#### Possibly related (weak matches — may be unrelated)` did\nnot pass the relevance floors and are often unrelated. Use one only when it\nis actually relevant to the question; if it is not, ignore it and do not\nmention it.\n\n## Active RAG\nWhen prior context would help, run a search proactively. **Always invoke via\n`{cli} --search "<keywords>"`** (optional `--since 2026-11-13`,\n`--until 2026-11-14`). To read the conversation around a hit, run\n`{cli} --recall-thread <turn_id>`. `{cli} --list` shows the newest records.\nThese commands are auto-allowed via `permissions.allow`.\n\n## Cut Context\nThe block is kept within a size limit. If it says that lower-ranked matches\nwere omitted, or ends with `_(cut here to keep this block within the context\nlimit.)_`, and the missing part is needed, run `{cli} --search` with the same\nkeywords: a manual search is not limited.\n\n## Deleting\nDelete only when the user explicitly asks: `{cli} --delete <id>` or\n`{cli} --delete-session <session_id>`. Both are irreversible and are not\nauto-allowed; confirm the id or session_id with the user first. They\nremove the memory from Redis only: the raw text stays in `audit.log` in\nthe data folder (no TTL); tell the user so when they ask to forget\nsomething.\n\n## Seven-Day Lifetime\nWhen the user expects something to be kept permanently ("remember this\nforever", 「ずっと覚えておいて」), the hooks save it as usual; add ONE short\nsentence in the user\'s language: Lite memories are deleted 7 days after they\nwere saved; for permanent storage use the Pro build or keep an external copy.\n\n## Recall Acknowledgment\nWhen recalled memory actually shapes your reply (you are recalling something\nsaved earlier), open the reply with a short acknowledgment in the user\'s\nlanguage — e.g. "Pulling this from earlier memory in this session." or\n「前回の回答がメモリに保存されています。」 If no relevant memory was found,\nor it did not influence your answer, do NOT announce anything.\n\n## Save Failures\nIf a `> ⚠️ N3MC notice:` line says that Redis could not be reached or that a\nrecord could not be saved, tell the user prominently and relay the command it\ngives (for example `docker start redis-stack`): until then, new messages are\nkept only in `audit.log`.\n'

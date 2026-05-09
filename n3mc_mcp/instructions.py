"""Behavioral instructions delivered to the LLM in the MCP initialize response.

Spec §5 — rules 0–9.  The full text below is the authoritative implementation;
the spec section summarises the intent, this file is the actual delivered text.
"""

INSTRUCTIONS = """\
N3MemoryCore — Lite (Ephemeral Memory)

This MCP server gives you hybrid-search memory (vector + BM25) backed by
Redis Stack. It is the free *Lite* build of N3MemoryCore: every entry
expires 7 days after it was saved. Treat it as a short-lived scratchpad,
not a long-term store.

BEHAVIORAL RULES
================

0. ROLE WHEN PRO IS ALSO CONNECTED (coexistence policy)
   If `n3mc-longtermmemory` (N3MemoryCore Pro, persistent SQLite+sqlite-vec
   backend) is also registered as an MCP server in the same session, treat
   the two servers as complementary, not redundant:

   - THIS SERVER (Lite, ephemeral) is the working-memory / scratchpad.
     Route here: in-session progress notes, temporary debugging state,
     throwaway intermediate results, transient task context — anything
     that will be irrelevant next week.

   - PRO is the canonical long-term store. Route THERE (not here):
     user profile / preferences, project specs and architecture decisions,
     world-building / character settings / design artifacts, API contracts,
     research summaries, outlines, and anything the user wants retained
     across sessions.

   - When in doubt, prefer Pro. "Painful if lost" > "fine if it vanishes".
   - Search order at the start of a turn: Pro first, then Lite.
   - Do NOT double-save the same fact to both servers.
   - When Pro is connected, the TTL warning (rule 7 below) is generally
     unnecessary — just route long-term content to Pro instead.

   If Pro is NOT connected, this server is the sole memory store. Accept
   all content, and emit the TTL warning (rule 7) when the user signals
   an expectation of permanence.

1. SEARCH FIRST, THEN ACKNOWLEDGE WHEN YOU USE MEMORY
   At the start of every user turn, call `search_memory` with a concise
   representation of the user's intent (key nouns + verbs). Use the
   returned snippets as context when formulating your answer.

   When your answer actually draws on retrieved memories (i.e. you are
   recalling or reusing information that was saved in an earlier turn),
   OPEN the reply with a short acknowledgment IN THE USER'S LANGUAGE.
   Examples:
   - Japanese: 「前回の回答がメモリに保存されています。」
                「以前の会話から該当情報を取り出しました。」
   - English:  "Pulling this from earlier memory in this session."
                "Recalling the setting you saved earlier."

   If `search_memory` returned nothing relevant, or you did not actually
   use the retrieved content, proceed normally with no announcement.
   Do not announce the mere act of searching — only the act of recalling.

2. SAVE AFTER EVERY SUBSTANTIVE EXCHANGE — AUTOMATIC, NO PERMISSION ASKED
   Saving is silent and automatic. The user should NEVER have to say
   "save this" or "remember that" — you save by default. Do NOT ask the
   user whether to save. Do NOT wait for the user to confirm. Just save.

   After every meaningful turn, call `save_memory` to persist:
   (a) The user's intent / question (short paraphrase, not verbatim).
   (b) YOUR OWN substantive output — decisions, plans, answers, and
       especially any creative or generated content the user may refer
       back to later: world-building, character settings, design sketches,
       code architecture, research summaries, outlines, etc. If you
       invested more than a sentence or two producing it, save it.
   (c) Key facts, preferences, and unresolved questions.
   Use one `save_memory` call per distinct fact (~50-200 chars each).
   Duplicates are auto-rejected, so err on the side of saving.
   NOTE: Lite memories expire 7 days after they are saved.

   Routing reminder: if Pro is also connected, long-lived content goes to
   Pro, not here. See rule 0.

   NOTE ON CODE BLOCKS: If the server config has `skip_code_blocks: true`,
   any content containing a triple-backtick fence (```) is rejected with
   `status: "skipped_code"`. The default is `false` (code is saved). If
   you see `skipped_code` come back, do NOT retry with the same payload —
   the user has opted out of code saving on purpose; save a prose
   description of what the code does instead. Do not announce the skip
   unless the user explicitly asks why a previous save did not stick.

3. LONG CONTENT — SAVE VERBATIM IN ONE CALL
   When the turn produces OR receives a long body (> ~400 chars) the user
   may want back verbatim — a pasted spec / log / code dump, or a long
   creative setting you just generated — pass the FULL text to a SINGLE
   `save_memory` call. The server creates a parent-document + chunks
   automatically and returns the full body on recall. Do NOT split long
   verbatim-worthy content into many short summaries.

   If Pro is connected, long verbatim content that the user may want
   months later belongs in Pro, not here.

4. TOOL-ERROR HANDLING — NEVER GENERATE LONG CONTENT BLIND
   If `search_memory` or `save_memory` returns a server error (Redis
   unreachable, timeout, "start Redis Stack" hint, etc.), STOP and
   announce the failure to the user in their language BEFORE generating
   any long creative or spec content. Otherwise every subsequent
   `save_memory` fails silently and the user loses their work when the
   session closes.

   Required on error:
   - Surface the error succinctly.
   - Relay the recovery hint the tool returned (e.g. the docker run command).
   - Ask whether to proceed WITHOUT memory or pause until backend is restored.

   Short factual replies may proceed, but still mention the error once.

5. SKIP NOISE
   Do not save greetings, confirmations, or purely mechanical acknowledgments.

6. RESPECT EXPLICIT REQUESTS
   If the user says "don't save this" or "forget that", call `delete_memory`
   and do not persist that content.

7. INFORM THE USER ABOUT THE 7-DAY TTL WHEN THEY EXPECT PERMANENCE
   The Lite build's 7-day TTL is visible to you (this instructions block,
   the `search_memory` description, and the `ttl_seconds` field in every
   `save_memory` response) but the USER does not see any of that. By
   default, silent auto-save (rule 2) is correct — do NOT mention TTL on
   every save. However, when the user's message signals an expectation of
   long-term retention, add ONE short sentence reminding them that Lite
   memories expire 7 days after they were saved, and suggest backing up
   externally if they want permanence. Do NOT ask permission to save;
   save anyway, then mention the TTL.

   IF PRO IS CONNECTED: prefer routing the long-term content to Pro
   (see rule 0) rather than emitting this warning. The warning is
   primarily for sessions where Lite is the only memory backend.

   Signals that warrant the reminder (examples, non-exhaustive):
   - Explicit permanence words: "remember this forever", "don't forget",
     "save this permanently", "keep this for next time", "次回も覚えていて",
     「ずっと覚えておいて」「永続的に保存して」「絶対忘れないで」
   - Pasting or generating a long setting / spec / code dump / world-
     building / character sheet that the user obviously invested time in.
   - The user referring back to content they saved more than ~5 days ago,
     where there is a real risk of imminent TTL expiry.

   Phrase the reminder in the USER'S LANGUAGE. Examples:
   - Japanese: 「なお、Lite 版のメモリは保存から7日で自動削除されます。
               永続保存が必要な場合は Pro 版（sqlite-vec バックエンド）を、
               もしくは外部バックアップをご検討ください。」
   - English:  "Note: Lite memories auto-expire 7 days after they're saved.
                For permanent storage, use the Pro build (sqlite-vec backed)
                or back it up externally."

   Emit the reminder ONCE per distinct long-term signal — not once per
   turn, not once per save. Do not repeat it if the same user has already
   been warned in the current conversation.

8. SESSION_ID AS PROJECT / TASK GROUPING KEY
   Both save_memory and search_memory accept an optional `session_id`
   argument. When multiple AI agents collaborate on the same project or
   task, they SHOULD pass the same `session_id` string (e.g.
   `"proj-alpha"`, `"task-refactor-auth"`) on every call.

   Effect in Lite (same contract as Pro): rows whose stored session_id
   matches the request's effective session get a ranking boost
   (b_session_match=1.0); non-matching rows are dampened
   (b_session_mismatch=0.6). Passing a consistent session_id therefore
   surfaces THIS project's memories above unrelated cross-project noise
   sharing the same Redis instance. The boost is multiplicative on top
   of the existing (cos·0.7 + bm25·0.3) · time_decay · b_local score.

   The value of passing session_id in Lite is therefore (a) ranking —
   pin recall to the current chat / project, (b) the filter key for
   `delete_memories_by_session` (tear down a finished project's
   memories without waiting for TTL), and (c) Pro migration
   compatibility (the same session_id keeps working when the workload
   moves to Pro's persistent backend).

   Precedence for session_id resolution:
   (a) Per-call `session_id` argument (highest precedence — use this when
       an orchestrator wants to pin child-agent calls to a specific
       project).
   (b) `N3MC_SESSION_ID` environment variable (set once at process
       start — use this when the whole Claude Code process is dedicated
       to one project).
   (c) Per-process UUIDv4 (fallback — fresh on each server start). Note
       that this fallback changes across restarts, so cross-restart
       recall of rows tagged with the previous run's UUID will receive
       the b_session_mismatch dampening unless you pin session_id
       explicitly via (a) or (b).

   When working on a named project or task, pass `session_id` on every
   save and search. For ad-hoc work, leave it blank.

9. BULK CLEANUP — `delete_memories_by_session`
   Use this tool to wrap up a finished project or reset a polluted
   workspace before TTL expiry. It removes every memory (singles,
   parent docs, child chunks, sha index entries) whose session_id
   matches. Scoped to the configured owner.

   Use cases:
   - Project closed and you want to keep search results clean.
   - A test/debug session generated noise that's hurting later recalls.
   - Reclaiming Redis memory early instead of waiting 7 days.

   Confirm the session_id with the user before calling — this delete
   is irreversible.

10. THREAD CONTEXT RETRIEVAL — `recall_thread` (§4.3.1)
    `search_memory` results include a `turn_id` field when the memory was
    saved with one. When a search hit lacks enough context to be actionable
    — for example, the snippet is a fragment without clear meaning on its
    own — call `recall_thread` with that hit's `turn_id` to retrieve the
    full thread of memories from the same conversation turn, plus N entries
    before and after it chronologically.

    Usage pattern:
      1. `search_memory` returns a hit with `turn=<some-id>` in the header.
      2. You judge the snippet insufficient for answering the user.
      3. Call `recall_thread(turn_id="<some-id>", before=2, after=2)`.
      4. The tool returns the turn's memories in chronological order with
         "▶" markers on the target turn's entries and unmarked neighbors.

    When saving memories, use the optional `turn_id` argument on every
    `save_memory` call in a turn so future `recall_thread` lookups work:
      - Choose any stable string for the turn (e.g. a short UUID, or
        "turn-1", "turn-2", …). Use the SAME value for all saves within
        one turn.
      - You do NOT have to tag every save; only do so when the turn's
        context might be worth reconstructing later (multi-step tasks,
        long creative sessions, research threads, etc.).

    The user does not need to be aware of `recall_thread`; it is an
    internal tool you use proactively to enrich recall quality.
"""

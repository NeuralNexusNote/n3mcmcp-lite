"""
MCP server instructions — behavioral guidance delivered to the LLM client
during the MCP ``initialize`` handshake.

These instructions implement the "auto-save" strategy: the client is told
to call ``search_memory`` at the start of each turn and ``save_memory``
after each meaningful exchange.

The Lite build's storage is **ephemeral** (Redis with 7d TTL), so the
instructions emphasise that expectation to the LLM client.
"""

SERVER_INSTRUCTIONS = """\
N3MemoryCore — Lite (Ephemeral Memory)

This MCP server gives you hybrid-search memory (vector + BM25) backed by
Redis Stack. It is the free *Lite* build of N3MemoryCore: every entry
expires 7 days after it was saved. Treat it as a short-lived scratchpad,
not a long-term store.

BEHAVIORAL RULES
================

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
   (c) Key facts, preferences, and unresolved questions established in
       the turn.

   Use one `save_memory` call per distinct fact. Keep each entry 50-200
   characters unless rule 3 applies. Duplicates are auto-rejected
   server-side, so err on the side of saving more, not less. Remember:
   Lite entries vanish after 7 days, so this is a rolling scratchpad —
   not a permanent archive.

3. LONG CONTENT — SAVE THE FULL BODY IN ONE CALL (verbatim recall)
   When the turn produces (or receives) a long body of text that the
   user may want back verbatim later — a user-pasted spec / article /
   log / code dump, OR a long creative setting / world / character
   sheet / design doc you just generated — pass the FULL text to a
   single `save_memory` call. The server automatically creates a
   parent-document + chunks (§3.11), indexes chunks for search, and
   returns the full parent body on recall so the user gets their
   content back exactly as written.

   Rough threshold: > ~400 characters → save as one full-body call.
   Shorter, summarizable content → follow rule 2 and save multiple
   short facts instead. Do NOT split a long verbatim-worthy body into
   many short summaries — that destroys recall fidelity.

4. HANDLE TOOL ERRORS VISIBLY — NEVER GENERATE LONG CONTENT BLIND
   If `search_memory` or `save_memory` returns a server error (Redis
   unreachable, connection refused, timeout, "start Redis Stack" hint,
   etc.), STOP and surface the error to the user in their language
   BEFORE generating any substantial creative or long-form content.

   Why: with the backend down, every subsequent `save_memory` call
   also fails silently. Producing a long setting / design / spec /
   code architecture / outline the user will want back later means
   they lose it the moment the session closes — and they will not
   notice the memory is broken until recall fails.

   Required on tool error:
   - Announce the failure succinctly in the user's language, e.g.
       JP: 「Redis Stack が停止しているようです。メモリ保存・検索が
            使えません。」
       EN: "Memory backend is unreachable — save and search are
            offline right now."
   - Relay any recovery hint the tool returned (e.g. the `docker run
     ... redis-stack-server:latest` command).
   - Ask whether to proceed WITHOUT memory (so the user knows their
     work will not persist this session) or to pause until the
     backend is restored.

   Short factual replies (one or two sentences) may proceed, but
   still mention the error once so the user knows saves are failing.

5. DO NOT SAVE NOISE
   Skip trivial greetings, clarifying questions, and mechanical
   acknowledgements. Save substantive content only: facts, decisions,
   user preferences, project context, unresolved questions.

6. RESPECT EXPLICIT REQUESTS
   If the user says "don't save this" or "forget that", comply — call
   `delete_memory` when asked to forget specific entries.

OPERATIONAL NOTES
=================
- Storage is ephemeral: 7d TTL per entry; nothing survives a fresh
  Redis container.
- Exact and semantic duplicates are auto-rejected by the server.
- Search ranks by: 0.7 * cosine_similarity + 0.3 * BM25, decayed by age
  and adjusted by importance.
- Importance auto-tunes from access frequency: every time a memory is
  returned in the top search hits, its access_count increments and it
  receives a small, capped boost on future queries. You don't need to
  do anything — frequently-useful memories naturally rise over time.
- The server is local — Redis runs on the user's machine (no cloud).
- For durable memory, the user can upgrade to the paid SQLite-backed
  N3MemoryCore build.
"""

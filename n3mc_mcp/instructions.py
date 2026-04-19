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

1. SEARCH FIRST
   At the start of every user turn, call `search_memory` with a concise
   representation of the user's intent (key nouns + verbs). Use the
   returned snippets as context when formulating your answer. Do this
   silently — do not announce the search.

2. SAVE AFTER EACH EXCHANGE
   After completing a meaningful response, call `save_memory` to persist:
   - The user's intent / question (short paraphrase, not verbatim)
   - Your key conclusions, decisions, or facts established
   Keep each saved entry short (50-200 characters). Use one `save_memory`
   call per distinct fact. Remember: entries vanish after 7 days.

3. EXTRACT FROM LONG PASTES
   When the user pastes a long text (spec, article, log, code dump), do
   NOT save it as one blob. Read it, extract each key fact as a short
   sentence, and call `save_memory` once per fact.

4. DO NOT SAVE NOISE
   Skip trivial greetings, clarifying questions, and mechanical
   acknowledgements. Save substantive content only: facts, decisions,
   user preferences, project context, unresolved questions.

5. RESPECT EXPLICIT REQUESTS
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

"""
N3MemoryCore MCP Server — Lite
================================

A Model Context Protocol (MCP) server that provides **ephemeral**
hybrid (vector + BM25) memory across conversations, backed by Redis
Stack with a 7d TTL per entry.

Tools:
  - search_memory   : hybrid search over stored memories
  - save_memory     : persist a memory entry (7d TTL)
  - list_memories   : list recent entries
  - delete_memory   : remove an entry by id
  - repair_memory   : re-create the RediSearch index if missing
"""

__version__ = "1.0.0-lite"

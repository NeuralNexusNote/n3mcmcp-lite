"""
N3MemoryCore MCP Server — Trial
================================

A Model Context Protocol (MCP) server that provides **ephemeral**
hybrid (vector + BM25) memory across conversations, backed by Redis
Stack with a 24h TTL per entry.

Tools:
  - search_memory   : hybrid search over stored memories
  - save_memory     : persist a memory entry (24h TTL)
  - list_memories   : list recent entries
  - delete_memory   : remove an entry by id
  - repair_memory   : re-create the RediSearch index if missing
"""

__version__ = "1.0.0-trial"

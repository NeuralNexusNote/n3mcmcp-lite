---
name: Bug report
about: Report a defect in the MCP server, search behavior, or installation
title: "[BUG] "
labels: bug
---

## Environment

- **OS**: (e.g. Windows 11 / macOS 14 / Ubuntu 24.04)
- **Python version**: `python --version` →
- **Redis Stack version**: `docker exec redis-stack redis-server --version` →
- **n3memorycore-mcp-lite version**: `pip show n3memorycore-mcp-lite | grep Version` →
- **MCP client**: (Claude Code / Claude Desktop / Cursor / Cline / other)

## What happened

A clear description of the unexpected behavior.

## What you expected

What you thought would happen instead.

## Steps to reproduce

1. ...
2. ...
3. ...

For search-quality issues, include a minimal reproducer:

```
save_memory: <payload>
search_memory: <query>
observed top-K: <ids/scores>
expected top-K: <ids/scores>
```

## Logs

Paste any relevant lines from the MCP server's `stderr` (Claude Code's
MCP panel surfaces this) or `pytest` output.

```
<paste here>
```

## Additional context

Anything else worth knowing — non-default config values, custom
`session_id`, multi-instance Redis setups, etc.

## Summary

What does this PR change and why?

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change to MCP wire surface or config schema
- [ ] Documentation / spec only
- [ ] CI / packaging

## Spec sync

- [ ] `N3MemoryCore_MCP_Spec_EN.md` updated (or N/A)
- [ ] `N3MemoryCore_MCP_Spec_JP.md` updated (or N/A)
- [ ] `README.md` + `README_JP.md` updated for any user-visible change
- [ ] `CHANGELOG.md` updated under `## Unreleased`

## Test plan

- [ ] `pytest tests/ -q` passes locally against Redis Stack
- [ ] `python -m build` succeeds and `twine check dist/*` is clean
- [ ] CI is green (will be checked automatically)
- [ ] If touching encoding / multilingual paths, ran the relevant test
      classes (`TestEncodingSafety`, `TestEncodingSafetyE2E`,
      `TestCjkBigramExpand`)

## Notes for reviewer

Anything reviewer-specific (decisions you want a second opinion on,
known limitations, follow-up work).

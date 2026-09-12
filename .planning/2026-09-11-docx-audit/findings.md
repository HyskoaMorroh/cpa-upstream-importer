# Findings

## Confirmed Environment
- All user-supplied paths resolve under the current Windows account.
- context-mode 1.0.169 passes initialization, hooks, storage, and FTS5 checks.
- RTK, graphify, memsearch, Codex CLI, and Claude Code CLI are installed.
- Caveman and cavecrew are installed skills, not standalone commands.
- The main repository has pre-existing changes in server.py, cpa_probe, web,
  tests, README, MEMORY.md, .gitignore, and untracked bulk/scrub modules.
- Legacy graphify-out/graph.json exists.

## Evidence Policy
Only current source locations, reproducible tests, and explicitly labeled
environment observations establish compliance. Historical memories and graph
edges are navigation aids, not proof that current code is correct.

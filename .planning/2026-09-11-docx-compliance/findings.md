# Findings

## Verified Environment
- All seven user-specified repository paths and the source Word document exist.
- Available executables: rtk, context-mode, graphify, memsearch, codex, claude,
  node, uv, Python, Go, Git, ripgrep, and Docker.
- context-mode doctor passes server initialization, FTS5, storage, Codex hooks,
  and MCP registration. Version: 1.0.169; Codex CLI: 0.154.0.
- memsearch collection resolved to ms_memsearch_a047a494. Relevant historical
  records exist; all code conclusions must be revalidated against this checkout.
- Ancestor AGENTS.md contains graphify navigation and graph refresh rules.

## Historical Leads, Not Current Proof
- Prior audits separated probing, writeback, API/web security, CPA contracts,
  test validity, and deployment/client integration.
- Prior notes distinguish CPA API credentials, CPA management credentials, and
  CPAMP admin credentials, and describe CPAMP as management/observability, not
  the model-request forwarding layer.
- Do not assume the historical graph or prior claimed fixes reflect current code.

## Current Evidence
- Source DOCX SHA256:
  02516EFA20DF487CA7A55BB1239C2059D12C1591AC43DAB73A23E5FEE815D6E9
- Parsed 18 content/image paragraphs and visually checked all 8 images.
- Saved 72 original worktree files, a hash manifest, and the initial diff in
  an external local artifact directory before application edits.
- Python graphify query works. A fresh code-only graph was generated outside
  the repository: 51 source files, 1247 nodes, 3319 edges.
- The npm TypeScript graphify command documented by ancestor AGENTS.md is not
  available; npx --no-install reports no executable. Do not silently install
  an unrelated same-name package or migrate tracked graph data.
- Initial investigators report reproducible gaps in bulk diff redaction,
  reload argument compatibility, profile body preservation, and duplicate
  YAML fields. Exact reports and tests are still being collected.
- The attached MHTML and second Desktop config exist. An investigator counted
  89 candidates and 356 protocol panels. Static MHTML checkbox attributes do
  not reliably preserve the user's dynamic selection state.

## Online Evidence
- Retrieved official Cloudflare, Anthropic, and OpenAI guidance into the
  context-mode index. Public-source notes are in research-notes.md.
- Current Cloudflare documentation specifies a default 125-second proxy read
  timeout. Treat the document's approximate 120 seconds as an incident symptom.
- Retry-After, jitter, attempt limits, and a total deadline matter together;
  changing a greeting cannot guarantee upstream acceptance or prevent bans.

## Evidence Location
Local working artifacts:
C:/Users/devin/vscode/artifacts/upstream-importer-20260911/

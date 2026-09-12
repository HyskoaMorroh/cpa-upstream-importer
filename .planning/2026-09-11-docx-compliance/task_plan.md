# Upstream Importer: Document Compliance

## Goal
Implement every actionable requirement in the supplied Word document in
upstream-importer, preserving compatibility with the read-only CPA and CPAMP
repositories and the supported editor/client integrations.

## Scope And Boundaries
- The Word document is the approved specification; do not replace its requirements
  with a newly invented design.
- Delegate broad repository exploration to isolated module investigators.
- Preserve pre-existing worktree changes and user data.
- Never edit the CPA, CPAMP, VS Code, cc-switch, Claude Code, or Codex repositories.
- Do not deploy, publish, send messages, delete existing data, or change live services.
- Redact credentials and personal data from reports and test evidence.
- Changes to deployment examples are permitted; live deployment files are read-only
  until the user confirms a deployment operation.

## Phases
### Phase 1: Specification And Tooling
Status: complete
- Extract paragraphs and tables with stable source identifiers.
- Verify rtk, context-mode, graphify, memsearch, and independent agent execution.
- Inspect prior planning state without overwriting it.

### Phase 2: Parallel Investigation
Status: complete
- Assign disjoint modules and cross-repository contracts to investigators.
- Capture baseline git status and tests.
- Produce a requirement-to-code-to-test matrix and report module findings.

### Phase 3: Reproduce And Implement
Status: in_progress
- Add failing tests for confirmed gaps.
- Apply narrowly scoped fixes in owned modules.
- Preserve behavior outside the approved specification.

### Phase 4: Integration Verification
Status: pending
- Run focused and complete tests, static checks, and local integration checks.
- Verify frontend workflows if changed.
- Independently review the final changes and check each requirement.

### Phase 5: Delivery
Status: pending
- Deliver a concise Chinese audit report with exact evidence and remaining limits.
- Provide local preview URL when a frontend dev server is needed.
- Do not claim VPS deployment or live validation without performing it.

## Decisions
- Interpret the user-specified tk as the available rtk output-compression utility,
  consistent with historical requests, while reporting the actual executable name.
- Native subagent tools are unavailable in this parent session; use local
  Codex CLI noninteractive workers with bounded reports and retained event logs.
- Use the supplied specification as prior design approval. No unrelated redesign.

## Errors
- No native context-mode MCP methods are exposed to the parent tool list.
  Use a local MCP SDK client connected to the installed context-mode server.
- Installed Python graphify CLI differs from the ancestor AGENTS.md's newer
  TypeScript command list. Verify both runtimes before choosing graph operations.

## Next Step
Complete and review transport fixes, then repair source/request contracts and probing.

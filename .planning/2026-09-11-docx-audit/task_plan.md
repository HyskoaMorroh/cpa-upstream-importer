# Upstream Importer Requirements Audit

## Goal
Audit every requirement in the supplied DOCX against the current dirty worktree,
repair confirmed defects in upstream-importer only, and record reproducible
evidence and any remaining deployment or external-product limitations.

## Boundaries
- Preserve all pre-existing modified and untracked files.
- Treat CPA, CPAMP, VS Code, cc-switch, Claude Code, and Codex repositories as read-only references.
- Do not send production requests, deploy, publish, commit, delete user files, or change live configuration.
- Do not expose credentials or personal data in reports.
- Delegate repository exploration; the coordinator reads compact evidence and specific edit targets.
- Do not update user memory files.

## Current Phase
Phase 1: Baseline and Requirements

## Next Step
Capture the existing worktree and dispatch read-only module investigators.

### Phase 1: Baseline and Requirements
Status: in_progress
- Verify paths and tool availability.
- Preserve baseline hashes and diff outside source files.
- Extract all document requirements with stable identifiers and source locations.
- Search relevant historical context without treating it as current evidence.

### Phase 2: Parallel Investigation
Status: pending
- Map every source module and assign coverage.
- Review ingestion, probing, writeback, API, frontend, deployment, and client integration.
- Reproduce confirmed findings using isolated local fixtures.

### Phase 3: Focused Repairs
Status: pending
- Add regression cases before each behavioral fix.
- Modify only responsible upstream-importer modules.
- Preserve external contracts and existing worktree changes.

### Phase 4: Verification
Status: pending
- Run the complete appropriate local test suite.
- Run cross-project contract and browser checks when applicable.
- Review the resulting diff independently.
- Verify coverage and identify non-verifiable production assumptions.

### Phase 5: Delivery
Status: pending
- Produce a Chinese requirement-by-requirement report.
- Record changes, verification results, tool use, and remaining risks.

## Decisions
- A dirty worktree is the baseline, not an error to reset.
- Local CLI agents will provide independent contexts because native spawn tools are not exposed.
- Existing graph state must be checked for freshness before being used as evidence.

## Errors and Limitations
- `tk` is not on PATH; `rtk` is installed. Identity remains to be checked.
- Native context-mode and subagent tool handles are not exposed in this parent session.

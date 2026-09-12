# Fix Ledger

Plan: C:/Users/devin/vscode/artifacts/upstream-importer-20260911/implementation-plan.md
Baseline: C:/Users/devin/vscode/artifacts/upstream-importer-20260911/baseline-files

## Investigation
All nine read-only investigators completed. Full reports are external local
artifacts named spec, probe, writeback, server, frontend, contracts, integration,
evidence, and baseline. The specification analyst identified 96 atomic checks.

## Task Status
| Task | Scope | Status | Evidence |
|---|---|---|---|
| T1 | HTTP redirects, decompression, total deadlines | fix round 2 review | five initial findings addressed; Unicode fixed; pinned 25 tests pass; deterministic phase tests |
| T2 | Dynamic source contracts, auth, conditional bodies, base paths | fix round 1 | 7 focused review findings; stable APIs retained |
| T3 | Protocol success, evidence isolation, profiles, inherited parameters, cancellation | retrying implementation | first CLI stream disconnected after RED; new worker reuses saved tests |
| T4 | Highest generation, all variants, fallback provenance, priority/proxy plan | independent review | pinned parent 21/21 pass; exact three-file package |
| T5 | YAML preservation, field merges, aliases, per-key capabilities, readback/recovery | independent review | pinned parent 38/38 pass; exact two-file package |
| T6 | Server/CLI bulk safety, serialization, reload order, concurrency, output privacy | implementing | exclusive server.py/cli.py/bulk.py ownership; additive frontend contract required |
| T7 | Frontend selection, exact confirmation, status, tier policy, accessibility | pending | frontend 1-16 |
| T8 | Isolated full tests, deployment/publication safeguards, tools/legacy/docs | pending | baseline 17/23-27; integration 8/9 |
| T9 | Full integration/browser/CPA contract tests and final audit | pending | all 96 source checks need evidence, not blanket success |

## Adjudicated Scope
- Probe report item 4's numerical 5x4 matrix was introduced by a mistaken task
  prompt, not the source document. It is not an acceptance requirement.
  Actual protocol, streaming, tools, and final CPA request parity still are.
- "tk" is interpreted as installed RTK based on prior project usage and local
  tool discovery. This interpretation was disclosed, not presented as a
  verified separate executable.
- The source permits log-based or guided diagnosis. Do not invent causality
  for either real example; CPAMP editor tests bypass the normal CPA executor.
- Keep single-file mount inode semantics. Replacing the config with a renamed
  temporary file would break the existing deployment.
- Local/remote source parsers must share coverage and identify uncertainty.
  Never blindly execute downloaded source or claim future arbitrary versions
  are already verified.
- Per-host priority and per-endpoint evidence are different concerns. Distinct
  credential permissions and channel paths must not be merged.
- User confirmation remains required for actual deletion and publication.
- CPA active-request timeout limit was checked directly: current HTTP clients
  are created without a configurable total/header-read timeout. Retry counts
  and keepalive settings cannot interrupt a silent response. Do not fabricate
  a YAML field or claim importer probe timeouts change CPA runtime behavior.

## Baseline Verification
- Safe isolated baseline suites: bulk 47, probe 603, pipeline 157, edges 133,
  reload 46, speed 36, web 237, tiering 213 checks passed.
- Full-redetection subset: 54 cases passed, 3 unsafe/external-dependent cases
  withheld. Server pure-function subset: 174 checks passed.
- Three local mock e2e tool scripts exited zero. Python 3.9 syntax checks and
  JavaScript syntax checks passed.
- These were subset results on the pre-edit baseline, not final full-suite proof.
- Dedicated final environment now uses Python 3.13.9, PyYAML 6.0.2, bcrypt 4.2.1.

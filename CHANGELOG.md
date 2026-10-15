# Changelog

## 2.2.0 — 2026-10-12

Verified on this build before release, in this order: `python tests/run.py` (synthetic, 6157 cases), `python tests/run.py <real config.yaml>` (6156), `python -I tools/scrub.py --check` (exit 0, all three layers executed), then a real container (this image + the real 296 KB `config.yaml` + the VPS compose topology: uid 0, `cap_drop: [ALL]`, single-file mount) driven through all four operator flows and a real browser.

### Fixed
- **A compat-only upstream was judged dead without a single request.** The section pre-scan treated model-family evidence as grounds for *excluding* a section; a catalog that listed only `gpt-*` names silently dropped `openai-compatibility` — the one section that worked. Family evidence now only ever *adds* sections; exclusion requires the upstream's own `supported_endpoint_types`. Fail-open otherwise.
- **Adding a key to an existing compat provider split the host into two providers by header differences.** The capability fingerprint serialized headers and request-scoped errors, so two keys of one host whose probes returned different `x-stainless-*` header counts were written as separate providers — the exact shape fixed on 2026-09-20, reintroduced. The fingerprint is now the model-name set only, which is the one field CPA keeps per provider (`OpenAICompatibilityAPIKey` has only api-key/weight/proxy-url). Only reproduced with the real `config.yaml`; the synthetic suite printed "跳过" and passed.
- **`gemini-3.1-pro-high` was dropped by its own bare base name.** The bare name ranked above every named effort tier (99), so `-high`/`-max` were collapsed as if they were cheaper. The bare name is now the standard tier: default-and-above all survive together; only `-low`/`-minimal` are collapsed, and only when something higher exists.
- **Frontend global error handlers were registered last.** Fifty-odd top-level `$('#id')` bindings run at parse time; any missing id threw before `window.onerror` existed, so the page went blank with no banner. Both handlers now register before the first DOM binding. Eleven unguarded `g.entries` / `p.sections` / `d.diffs` accesses are guarded; one malformed group no longer disables the five bulk-management action buttons.
- **全局调优体检 hid its only commit button when `tuning_id` was absent**, while rendering the full advice list. The preview now shows whenever there are pending changes; a missing `tuning_id` disables the button and says why instead of hiding the panel.
- **Three probe events (`prescan`, `prescan-miss`, `section-gate`) had no renderer**, so a section skipped by the pre-scan vanished from the stream with no explanation.
- **`/favicon.ico` returned 401.** Browsers request it without a token; every page load logged a red console error that buried real ones. It now returns 204, and `index.html` declares an empty icon so the request is not made at all.
- **Any `?v=` value earned a one-year `immutable` cache header.** Only a version equal to the current content hash does now; stale versions still serve the current file but as `no-store`. A stale key could otherwise pin an old `app.js` against a new server for a year — the mechanism behind error strings that no longer exist in source.
- **The bulk-tier stamp contradicted the line it stamped.** `priority: 123 # … 原档 122` — value changed, comment kept. The stamp now records `122 → 123` and says the trailing comment is the pre-change rationale. Stamp detection no longer depends on the note text, so repeated edits replace rather than accumulate (the 2026-10-06 regression would otherwise have returned).
- **Bulk "统一优先级" no-op was invisible and misleading.** The header counts cross-section tier differences while the button unifies within a group; on the real config every group was already uniform, and the only feedback was a low-contrast line. The message is now prominent and names the dimension mismatch and the control that actually edits cross-section tiers.
- **An unopenable probe ledger produced no diagnostic.** The fail-closed `ledger_unavailable` path swallowed the exception; every section read "未实测" with zero requests and nothing in the log said why. It now logs the exception type, message and the `IMPORTER_PROBE_LEDGER` hint. No credentials or bodies are logged.
- **A failed model ranking silently fell back to alphabetical order.** `_probe_order` wrapped `rank_models` in a bare `except` — and its own docstring names alphabetical order as the cause of "勾了 gpt-5.6 却没勾 gpt-5.6-sol" (older names consume the per-section quota first; same-generation variants are never reached). The fallback stays, but it now logs a warning and emits a `rank-fallback` event that the stream renders.

### Performance
- **The synchronous catalog fetch is now single-flight.** On a cold cache the four section threads each started their own thread pool against both catalog mirrors (up to 12 s each) and raced each other on the disk-cache `os.replace`; with GitHub unreachable this repeated every 10 minutes (`_TTL_BAD`) for the whole batch. Callers now re-check the cache under a lock and only the first one fetches. `use_cache=False` (forced refresh, isolated tests) deliberately bypasses the lock. No request budget or verdict changes.
- **The redaction gate crashed before its structural and tracked-file layers ran on Windows** (`⚠` is unencodable in GBK), and its filename whitelist never matched (`.match()` anchors at 0, pattern began with `\.`). Both fixed; layers 2 and 3 have now executed on this machine for the first time.
- **Two test suites were never registered** and one had no entry point and no `sys.path` bootstrap: 19 cases that had never run (`test_body_rule_boundaries.py`, `test_profile_failure_evidence.py`). Registered and running.
- `test_edges.py` ⑨⑩ pinned "one provider row per host", which contradicts `test_config_preservation_regressions.py` and CPA's provider-level `models`. They now pin the actual invariant: same base-url and same priority (one bucket, one tier), regardless of row count.

### Added
- `tests/test_server.py`: Cache-Control, Content-Length, favicon and version-stamp assertions. The suite previously discarded response headers and had zero cache-policy assertions.
- `tests/test_ledger_failure_diagnostics.py`, `tests/test_effort_tier_gate.py` additions, `tests/test_writeback_compliance.py` bucket/tier invariants.
- `.gitignore`: `.memsearch/`, `.playwright-mcp/`, `**/*handoff*.md`. The first two were untracked *and* unignored and contained a private infrastructure subdomain, upstream domains and a Windows account name.

### Known
- One credential's inference 401 still stops the whole provider, so sibling keys on the same host are never probed (`attempts=0`). This violates the "一个 KEY 不能拖垮全部" requirement. The classifier scope is provider-wide by schema; a per-credential stop scope is in progress.

## 2.1.0 — 2026-10-04

### Fixed
- **Restore active probing by default.** 2.0.0 made probing passive unless the operator hand-authored a site allowlist. Because the shipped `.env` points `IMPORTER_PROBE_POLICY_FILE` at a path that does not exist, every upgraded deployment silently became zero-request: adding accounts, single-site diagnosis and full re-detection all returned "未实测" for every section while the UI still announced "② 探测完成 · 0 次请求". A risk control that removes the feature is not a risk control. Probing a target the operator explicitly asked for is now permitted without any policy file (`IMPORTER_PROBE_MODE=managed`, the default); the per-request limits below are what reduce ban risk. `IMPORTER_PROBE_MODE=passive` keeps the analysis-only behavior for troubleshooting, and an explicit policy file still overrides everything.
- **The probe ledger could not be created in the shipped topology.** The image chowned `/backups` to `APP_UID` with mode 0755 while the compose runs as uid 0 with `cap_drop: [ALL]`; without `CAP_DAC_OVERRIDE` root is an ordinary user and cannot create `probe-policy.sqlite3` in a directory it does not own. The fail-closed ledger then denied every upstream request. `/backups` is now mode 1777 so any configured uid can create its own ledger without being able to delete another's.
- Report a blocked ledger instead of claiming authorization. `summary()` previously reported "已配置 N 个站点的主动探测许可" while the ledger was unwritable and every request was being refused.
- A finished job with zero requests is no longer labelled "探测完成", and a job with policy-skipped sections is labelled "探测部分完成" with the skipped count. The static banner no longer asserts a mode before `/api/context` has been read; absent metadata reports an unknown state rather than guessing.
- Count passed end-to-end verifications by their own result. The receipt derived "通过" as `total − hard failures`, and because policy-skipped items are excluded from `verify_failed`, a run where all 22 items were skipped displayed "22/22 通过 —— 真打了一次业务请求" above 22 rows that each read "失败 · 未实测".
- Render `policy-wait` events. They fell through to the unrecognized-event fallback and printed raw JSON, so a wait for a shared provider slot looked like a defect.
- Honor cancellation while waiting on another thread's in-flight probe, and poll in short intervals instead of blocking for `timeout + 30` seconds, so stopping a job no longer leaves the UI polling a task that keeps waiting.

### Changed
- Managed mode registers a target only when a request for it actually arrives; it never scans configuration or probes in the background. Budgets, the single-flight lock and the minimum interval are keyed per site and shared across credentials and proxies, so rotating a key cannot widen a limit. An explicit restriction (403/429, anti-probe or client-only bodies) still stops that provider persistently.

## 2.0.0 — 2026-10-02

### Changed
- Upstream probing is passive by default. Active probing now requires explicit provider authorization and a persistent request budget. This is a breaking default change; existing deployments remain usable for configuration analysis and writeback without granting probe permission.
- Catalog requests, generation requests, retries, and WebSocket handshakes share provider and credential budgets. Automatic HTTP redirects are refused to prevent hidden requests.
- Provider restrictions stop subsequent probes. The importer does not rotate credentials, proxies, or client profiles to continue after a recognized restriction.
- Untested entries are not automatically activated, including the CLI write path. Existing configuration entries and disabled state remain preserved during passive rebuilds.
- Writeback and CPA management reload are reported separately from upstream verification. Unbound gateway generation checks are skipped rather than reported as verified.

### Fixed
- Preserve evidence already obtained when a later probe is stopped by policy.
- Keep the configured model list untouched when a section was never probed this round. Reordering it from market guesses silently replaced lists built from earlier real probes, and the entry stayed usable so nothing surfaced until a request routed to a model the site never offered.
- Propagate a restriction from the last capability request into the plan immediately.
- Read bounded WebSocket rejection bodies and handle failed ledger settlement without reporting clean success.
- Preserve completed source identity snapshots for probe jobs; keep display-only catalog refresh nonblocking and isolated by source options.
- Prevent ambiguous POST replay and contain deadline errors within the transport response contract.
- Require Node runtime tests at the release gate. Public tests no longer depend on a private domain mapping file.

### Security
- The compatibility section uses the generic `OAI` fallback prefix only when no existing configured prefix can be reused; existing configured prefixes remain authoritative.
- Provider rules and ledgers stay outside public Git history and image build context.
- The private domain map is separate from the public sanitizer; missing maps cannot trigger a write operation.
- Local testing cannot establish a universal safe probe rate. Operators must follow each provider's actual permission and limits.

# Changelog

## 2.0.0 — 2026-10-02

### Changed
- Upstream probing is passive by default. Active probing now requires explicit provider authorization and a persistent request budget. This is a breaking default change; existing deployments remain usable for configuration analysis and writeback without granting probe permission.
- Catalog requests, generation requests, retries, and WebSocket handshakes share provider and credential budgets. Automatic HTTP redirects are refused to prevent hidden requests.
- Provider restrictions stop subsequent probes. The importer does not rotate credentials, proxies, or client profiles to continue after a recognized restriction.
- Untested entries are not automatically activated, including the CLI write path. Existing configuration entries and disabled state remain preserved during passive rebuilds.
- Writeback and CPA management reload are reported separately from upstream verification. Unbound gateway generation checks are skipped rather than reported as verified.

### Fixed
- Preserve evidence already obtained when a later probe is stopped by policy.
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

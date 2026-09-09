# Provider evidence — connect, verify, collect, join, and revoke

## The question

> Which external evidence can change the order of this technical backlog, and what would make it
> safe to use?

A token or API key only says that a local machine has configuration. It does not establish which
account it reaches, which property the account may read, whether a response was sampled, or
whether the data belongs to this audit. Keep those questions as separate steps.

## The chain

**1. Inspect configuration without exposing it.**

Use `provider_registry()` to see each provider's credential components, access class, operations,
quota mode, and privacy class. Use `provider_doctor()` to distinguish `not_configured`,
`credential_present`, and `not_required`. Neither operation contacts a provider, and neither says
that credentials have been verified.

**2. Connect a read-only grant deliberately.**

Store secrets outside the repository in the configured local credential location. GSC's durable
grant accepts only `webmasters.readonly`; the refresh token, client secret, and raw property names
remain in a local `0600` file. DataForSEO backlinks remain disabled until account eligibility, a
cache key, a spend ceiling, production approval, and cost approval are all declared. IndexNow is a
separately confirmed write action and is not part of provider collection.

**3. Verify the account and the selected target separately.**

Call `provider_verify(provider, request)` as one explicit read. Its `authenticated_account` field
means the provider accepted the credential. Its `target_access` field is independently
`verified`, `not_granted`, `not_requested`, or `unknown`; an account listing does not prove access
to an arbitrary requested property, host, counter, or site. Unknown scopes and quota state stay
`unknown` rather than being reported as empty or unlimited.

**4. Collect one bounded operation.**

Call `provider_collect(provider, operation, request, artifact_dir=...)`. It produces a
`seohead.provider-evidence.v1` envelope with a status of `complete`, `partial`, `failed`, or
`skipped`. GSC analytics is locally row-bounded; URL Inspection is a declared sample, never an
index census; PSI is a bounded mobile-first lab sample; CrUX History is field data; GA4 sessions
are not search clicks; Metrika raw Logs have no route. A supplied `artifact_dir` keeps raw rows and
identifiers in a restricted local artifact. The public envelope has a null target reference and
redacts filter values.

**5. Join after collection, without changing a crawl.**

Call `provider_join(crawl_pages, evidence_rows, review_external_only=...)`. Exact URL matching is
the default. The result preserves `matched`, `crawl_only`, `external_only`, and `unkeyable`
populations. `list_crawl_candidates` exists only after `review_external_only=true`; it does not
change a frontier or call an external-only URL orphaned by itself.

A priority adjustment must name its source fields, period, coverage, and adjustment. `partial`,
`sampled`, `truncated`, `unmatched`, and `privacy_thresholded` evidence is unavailable for that
purpose, never zero. An accepted adjustment has `technical_severity_changed: false`: it changes
work order, not the underlying technical finding.

**6. Refresh or revoke without hiding a change.**

Refresh a durable GSC grant only for an explicit provider operation. If a grant expires or a
provider returns an authorization failure, record `failed` or `not_configured`; do not reuse old
results as current evidence. Revoke the grant at the provider, then remove the restricted local
grant file and rerun `provider_doctor()` to confirm `not_configured`. Retained evidence keeps its
retrieval time and local artifact reference, so it remains historical rather than silently current.

## What comes out

A provider evidence envelope proves what was asked, when it was retrieved, its coverage limits,
and whether it is usable. It never proves a clean technical result just because a provider was not
configured, a target was not granted, or a result was truncated.

## What it cannot answer

- **Whether an authenticated account owns a requested target.** Only `target_access: verified`
  establishes that selected target.
- **Whether a sampled or thresholded traffic value is zero.** It is unavailable prioritization
  evidence.
- **Whether a URL is orphaned.** External-only means only that the current crawl did not match it.
- **Whether IndexNow caused indexing.** A receipt records a submission, not an indexing outcome.

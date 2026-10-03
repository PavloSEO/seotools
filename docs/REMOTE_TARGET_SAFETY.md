# Target safety for queued remote scans

`seohead.recon.remote_policy.RemoteEgressPolicy` is the security boundary for a
future authenticated scan service. It does not start a service or a worker. The
service must construct one policy from trusted, project-owned configuration,
inject `policy.authorize_submission` into its submit boundary, and reject a
missing policy. Before invoking the existing crawl handler, the worker must use
`with policy.checked_job(project_id, target_url, effective_config): ...` for the
complete job. The policy checks DNS at submission and again at dispatch; the
shared HTTP transport checks and pins the target on every physical request.
This includes redirects and HTTP requests fulfilled for the browser. The bound
transport retains the policy across crawl worker threads and browser callbacks.

Remote policy ignores `SEOHEAD_ALLOW_PRIVATE_NETWORKS` and
`SEOHEAD_ALLOW_PRIVATE_HOSTS`, which remain local CLI/MCP choices. A service
operator may configure exact private staging hostnames for a project. Such a
hostname may reach RFC1918 IPv4 or IPv6 ULA addresses; loopback, link-local,
metadata, multicast and other non-global ranges remain blocked. An allowlisted
name does not authorize subdomains or a redirect to a different hostname. The
request is connected to the address just validated, so a second DNS answer
cannot replace the vetted target. A DNS error or mixed public/private answer
fails before connection. Internal staging access therefore still requires an
explicit trusted project policy and network isolation appropriate to the
service operator.

`max_requests_per_origin` and `max_total_requests` are counters shared by every
client bound to one policy. Each physical HTTP attempt spends a slot, including
redirects, retries, sitemap children, resource fetches and browser HTTP routes.
The policy also rejects crawl settings with an unbounded request limit or a
concurrency/URL limit above the trusted policy, or a delay below its configured
floor. Budget exhaustion has a named
error and is never reported as a clean measurement. The future worker must
translate that error into partial/failed job evidence and preserve the scan's
actual finish reason.

Queued remote jobs currently reject proxy routing and alternate browser
backends/profiles. A proxy endpoint or remote browser can change the true
network destination; support requires a separate proof that the same policy
governs that transport. The local CLI/MCP proxy feature may still be used under
its own explicit policy. Remote jobs force `trust_env=False` when constructing
the shared HTTP client, so ambient proxy variables cannot silently reroute a
tenant's job. TLS verification remains enabled. No browser request may fall
back to Chromium's own network path; unsupported browser requests remain
blocked with an explicit limitation.

Submission URLs, redirect URLs, exception causes, credential-bearing headers
and browser request bodies must not be serialized to service events or logs.
Use a job ID and the safe `RemoteTargetError.code`/message for operational
events. Retained scan evidence is protected by project authorization and its
existing redaction/retention policy; a service must not treat it as an
operational log. API authentication, project authorization, queue isolation,
artifact access and worker lifecycle are provided by the adjacent remote
service issues, not by this policy object alone.

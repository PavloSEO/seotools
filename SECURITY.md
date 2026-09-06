# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub private vulnerability
reporting:

<https://github.com/PavloSEO/seotools/security/advisories/new>

Include the affected command or MCP tool, version, minimal reproduction, expected impact, and any
safe mitigation you have already tested. Do not include real credentials or client crawl data.

## Security model

SEOHEAD Tools is a local CLI and stdio MCP server. It has no hosted control plane, user account,
telemetry collector, automatic updater, or inbound network port.

The main trust boundaries are:

- remote HTML, XML, headers, redirects, robots.txt, and provider responses are untrusted data;
- URL tools block private and non-public network targets unless explicitly enabled for an
  authorized staging or intranet environment;
- URL fetches and direct TLS probes resolve once, reject non-global addresses by default, and
  connect to the vetted address while retaining the original hostname for SNI and certificate
  verification, so a resolver cannot answer the check and the connection differently;
- addresses that carry a non-public destination inside a globally-scoped form — NAT64, 6to4,
  IPv4-mapped — are judged by the address they carry, not by the wrapper;
- file-producing tools should run inside a dedicated working directory or mounted container
  volume;
- operations that probe paths, rewrite files, verify bots over DNS, or spend provider credits
  require explicit input;
- credentials are loaded from the local environment/configuration and should never appear in
  prompts, issue reports, transcripts, or committed files.
- authenticated crawl headers use host-bound environment references; the global
  `http.headers` setting rejects credential-bearing header names.

## Supported versions

Security fixes target the latest `3.x` release and the current `main` branch. Versions older than
`3.0.0` are not supported. Include the affected version and commit SHA in reports.

## Out of scope

- findings that require deliberately disabling documented guardrails;
- third-party service outages or inaccurate provider datasets;
- vulnerabilities already fixed by a current direct dependency release;
- SEO disagreements without a confidentiality, integrity, or availability impact.

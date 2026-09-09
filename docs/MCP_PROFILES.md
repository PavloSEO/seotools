# MCP profiles and caller-requested progress

`configure_profile(server, profile)` runs after tool registration and removes schemas through
FastMCP's public `remove_tool()` method. Build a fresh server to choose another profile.

| Profile | Advertised surface |
|---|---|
| `full` | Compatibility mode: every registered tool remains available. |
| `audit` | Audit workflow, catalog, and bounded SF audit tools. |
| `infra` | Inspect/catalog plus domain, CDN, technology, security, robots, sitemap, and regions. |
| `quick-check` | Inspect/catalog plus small URL checks. |
| `router` | Inspect, audit workflow, and catalog only. |

The root bindings provide `seo_inspect_url`, `seo_audit_workflow`, and `seo_tool_catalog` before
selecting a restricted profile. `seo_tool_run` is intentionally excluded from `router`: a generic
action-and-any-payload tool would bypass real tool schemas and their paid, write, and network
guards. The catalog may describe allowed low-level tools without advertising every one as an MCP
schema.

`install_progress(server)` returns a factory for async bindings. Inside a tool request, use
`async with progress.scope(ctx, "crawl-site") as report:` then call either
`await report.known(done, total)` for measured totals or `await report.elapsed()` when no total is
known. Notifications are sent only when the caller supplied an MCP `progressToken`; elapsed
messages label the unknown total, values are monotonic and throttled, and context exit stops
emission on success, cancellation, or failure. Progress does not guarantee client timeout or
completion behavior.

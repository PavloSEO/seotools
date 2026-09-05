# Recovering an interrupted native crawl

`crawl-site` checkpoints as it goes. This is the route to resume one that stopped early instead
of starting over — what the checkpoint is, the one requirement for a clean resume, and how to
tell a successful resume apart from an intentional fresh start.

## The question

> `crawl-site` got killed (Ctrl-C, an OOM, a closed laptop lid) partway through. Can I pick up
> where it left off, or do I have to refetch the whole site?

## The checkpoint

Every crawl with an output directory writes `crawl_state.json` there as it runs — the frontier
still queued, the URLs already seen, the exclusion tally, the query-variant budget, and a
fingerprint of the settings that were in effect. It is plain JSON, written atomically, and it is
the only thing a resume reads. Losing it, or running without `--out-dir` at all, means the next
run has nothing to resume from and starts fresh — there is no other recovery path.

## The one requirement: an identical invocation

Rerun the exact same command: same `--out-dir` (so it finds `crawl_state.json` where it left
it), same start URL, and the same effective configuration — same config file, same flags, same
environment. `crawl-site` fingerprints every results-affecting setting
(`seohead crawl-site --config-help` lists which ones) into the checkpoint, and compares it
against the fingerprint of the settings this invocation is about to apply.

```bash
seohead crawl-site --url https://example.com --config ./crawl.json --out-dir ./run
```

If it stops early — Ctrl-C, a crash, the process killed — run that identical line again.
Nothing about resuming is automatic beyond that: there is no separate `--resume` flag, because
the checkpoint's own presence and matching fingerprint *are* the resume decision.

## Reading the result: resume, or intentional fresh start

Check these fields after the second run, in this order:

| Field | Where | What it tells you |
|---|---|---|
| `resumed` | top-level result / `run.crawl_resumed` in `audit.json` | `true` if `crawl_state.json` matched and was loaded; `false` if the crawl started over |
| `discovery.resume_note` | top-level result | the exact reason — resumed with a queue/seen count, or why not |
| `finish_reason` | top-level result / `run.crawl_finish_reason` in `audit.json` | `finished`, or why the crawl stopped: `interrupted`, `errors`, `url_limit`, `duration_limit`, `robots_unavailable` |

A successful resume looks like:

```json
{"resumed": true, "discovery": {"resume_note": "resuming from checkpoint: 4 URL(s) queued, 6 seen"}}
```

An **intentional** fresh start — not a bug — looks like `resumed: false` with a `resume_note`
naming the reason: no checkpoint file, a different start URL, a schema this build does not
recognise, or **"crawl scope or limits changed since the checkpoint"**. That last one is the
case worth calling out: changing a results-affecting setting (`--max-urls`, `--max-depth`,
robots policy, scope, and everything else `--config-help` marks results-affecting) between the
interrupted run and the retry invalidates the checkpoint on purpose — a resume built on a
different rule set than it is about to apply would silently mix the two. It refetches from the
start URL, exactly like a first run, and that is the correct behaviour, not a defect.

Inspect `run.crawl_resumed` and `run.crawl_finish_reason` in `./run/audit.json` after the second
run. Use `seohead log-scan --run ./run` to find contradictions among the run's recorded facts.

## What resuming does and does not promise

An identical retry reuses recorded completed page results and continues with the queued frontier.
`crawl_state.json`'s `seen` set is discovery state, not proof that each URL was persisted as a
completed page result. A request still in flight when an interruption or circuit breaker stops
the crawl, and whose result was not recorded, may be repeated on the retry.

## What it cannot answer

- **Whether a checkpoint from one machine or user is safe to resume on another.** The state
  directory must not be world-writable (`crawl-site` refuses one that is); it also does not try
  to detect malicious files or a different security context.
- **Resuming across a code upgrade that bumps the checkpoint schema.** A schema mismatch is
  treated the same as no checkpoint: a fresh start, never a partial or best-effort read.
- **Zero refetch after an interruption or circuit-breaker stop.** Recorded completed page results
  are reused, but unfinished or unrecorded in-flight requests may be repeated.

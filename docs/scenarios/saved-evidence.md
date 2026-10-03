# Saved evidence: inspect first, extract offline, mutate only with a backup

Use a validated saved scan when the next question can be answered from retained
evidence. Reading a section or applying an ad-hoc extraction rule does not
fetch a URL and does not alter the artifact.

## 1. Inspect the retained evidence boundary

```bash
seohead scan evidence --scan native.sqlite --section capabilities
seohead scan evidence --scan native.sqlite --section corpus --limit 100
seohead scan evidence --scan native.sqlite --section structured --limit 100
```

An unavailable corpus, body, rendered representation, or route counterpart is
not a clean result. The response names retained coverage and reasons before a
specialist uses it for a conclusion.

## 2. Extract only from retained complete bodies

```bash
seohead scan extract --scan native.sqlite --input '{
  "rules":[
    {"id":"product-name","kind":"text","selector":"h1","operator":"matches","value":"Product *","max_matches":1}
  ]
}'
```

`matches` is a bounded `*`/`?` glob, not regular expression execution. The
response keeps counts and reports a rule as unavailable when an oversized or
capped candidate cannot be safely matched. This ad-hoc result is not written
back to the scan.

## 3. Resolve retained fragment links

```bash
seohead scan fragment-links --scan native.sqlite --state missing --limit 50
```

A `#fragment` is broken when the retained destination document contains no
matching element — not when the destination URL fails to load. Static and
rendered bodies are checked independently, and a destination that was never
retained (or was retained truncated, non-HTML, or failed) is a named skip,
never a broken bookmark.

## 4. Review mutations separately

`scan-requeue` and `scan-import-urls` are explicit mutation boundaries. They
do not run because an evidence read happened, and each requires a newly created
verified backup destination after the restricted selection has been reviewed.

## Acceptance

- Evidence reads state their retained population and no network request occurs.
- Offline extraction names unavailable source bodies instead of treating them as absent values.
- Any requeue or import has a newly created backup that can be inspected before further collection.

## Covers

- **URL** — Broken Bookmark

Saved evidence is otherwise a reader and operator-control workflow; it does
not add a separate SF issue-catalogue finding.

## What it cannot answer

It cannot recreate a body that retention omitted, prove a live target has not
changed since capture, or establish an uncaptured representation as clean.

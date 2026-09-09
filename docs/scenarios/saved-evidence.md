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
seohead scan extract --scan native.sqlite --json-input '{
  "rules":[
    {"id":"product-name","kind":"text","selector":"h1","operator":"matches","value":"Product *","max_matches":1}
  ]
}'
```

`matches` is a bounded `*`/`?` glob, not regular expression execution. The
response keeps counts and reports a rule as unavailable when an oversized or
capped candidate cannot be safely matched. This ad-hoc result is not written
back to the scan.

## 3. Review mutations separately

```bash
# Each mutation needs a new backup path; review the selection first.
seohead scan requeue --scan native.sqlite --where 'status_code = 500' --backup native-before-requeue.sqlite
seohead scan import-urls --scan native.sqlite --urls-file review-urls.csv --backup native-before-import.sqlite
```

`scan-requeue` and `scan-import-urls` are the explicit mutation boundaries.
They do not run because an evidence read happened, and they refuse to proceed
without their mandatory verified backup destination.

## Acceptance

- Evidence reads state their retained population and no network request occurs.
- Offline extraction names unavailable source bodies instead of treating them as absent values.
- Any requeue or import has a newly created backup that can be inspected before further collection.

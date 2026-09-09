# Project control: prepare an inspectable local audit workspace

Use this when a specialist needs one project folder that states what ran, what
did not run, and what evidence still needs review. It does not turn a prepared
workspace into a completed audit or a delivered report.

## 1. Read the available playbooks

```bash
seohead skill list
seohead scenario show --name provider-evidence
```

Use an unambiguous full identifier when a short name is ambiguous. These calls
only retrieve packaged guidance; they do not run a crawl.

## 2. Create and prepare the workspace

```bash
seohead project start \
  --directory ./example-project \
  --target https://example.com
```

The preparation path initializes the local checklist, applies the default
bounded crawl policy, and writes an initial plan. A failure or partial crawl
remains recorded as such in `preparation.json` and project status.

Supply candidate competitors only when their source is known. They are local
candidate workspaces, not evidence that they rank or compete:

```bash
seohead project prepare --directory ./example-project --input '{
  "competitors":[
    {"url":"https://competitor.example/","source":"operator shortlist","observed_at":"2026-09-09T00:00:00Z"}
  ]
}'
```

## 3. Review policy before widening scope

```bash
seohead project policy --directory ./example-project
seohead project status --directory ./example-project
```

`project-policy` previews a data-only policy. Applying a changed policy requires
the current `expected_revision`; a crawl above the project thresholds requires
the explicit `approve_large_crawl` input. Record a checklist result only from
supplied evidence, with its own expected revision.

## Acceptance

- `project-status` names the retained scan or names the crawl as not run/partial.
- Every competitor has an operator source and stays a candidate until separately measured.
- The policy revision, preparation state, and checklist coverage can be reviewed from the local workspace.

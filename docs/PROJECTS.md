# Projects

Projects keep independent `scan.v1` artifacts, reports, and a small provenance-backed site envelope together.

```bash
seohead project new --directory ./example-project --target https://example.test/ --label "Example"
seohead project status --directory ./example-project
```

The directory contains `project.json`, `scans/`, `reports/`, and `log.md`. Project creation does not crawl a site, execute templates, initialize a checklist, or claim audit completion. Until checklist setup lands, status reports `not_initialized` rather than a 0/0 figure.

# Deliverables: what separates an analysis from a result

A finding is not a deliverable. "Your images are heavy" is an opinion; an archive of re-encoded
files with the saving measured per file is a job that is already done.

## The chain that shows it

```bash
seohead images-download --urls "<comma list>" --output-dir ./original
seohead images-optimize --files ./original --output-dir ./optimized \
        --max-width 1920 --quality 82 --format keep
tar -czf images-optimized.tgz ./optimized
```

Real numbers from one run: 10 files, 7.92 MB → 2.58 MB, −67%. The archive is the deliverable.
It does not fix a server with no compression configured — but it proves the server has none,
with the bytes to show for it.

`docs/scenarios/` holds 59 chains in this shape, each with its commands, its output, its cost
and its limits.

## Choosing the format

| Format | Audience | Command |
|---|---|---|
| `docx` | the client — prose, severity, meaning | `report-build --format docx` |
| `xlsx` | the SEO — sheets, filters, a chart | `report-build --format xlsx` |
| `csv` | the tracker — one row per task | `report-build --format csv` |
| `md` | a repository, or another agent | `report-build --format md` |
| `audit.json` | the machine-readable original | written by the crawl |

All of them derive from one document, so the client's PDF and the developer's ticket cannot
drift apart.

## What belongs in every deliverable

- The config file the crawl used. Without it the numbers cannot be reproduced or compared.
- The coverage sentence next to any score.
- What was skipped, and why.
- The limits, stated rather than implied. → [reference/limits](../reference/limits.md)

## What is not yours to decide

Severity is not priority. A critical on a page nobody visits ranks below a warning on the page
that makes the money, and nothing in this toolkit knows which is which. Order by severity,
say so, and let somebody with traffic data reorder it.

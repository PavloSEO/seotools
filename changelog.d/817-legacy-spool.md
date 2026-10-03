- Legacy `crawl-site --out-dir` now keeps page, link and form evidence in
  append-only JSONL sidecars during collection and resume instead of retaining
  duplicate in-memory lists. Checkpoints verify sidecar counts; older v4
  inline-form checkpoints migrate without losing form findings. The existing
  audit bridge names its page/form/link materialization bounds and preserves
  collected evidence when an audit is unavailable. A prior report is retained
  under a stale filename rather than presented as the current run. Native
  JavaScript escalation reads page records through a re-iterable SQLite view
  instead of rebuilding all page objects. Full large-audit streaming remains
  tracked by #816/#817.

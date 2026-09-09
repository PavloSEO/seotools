-- scan.v2 is an explicit write upgrade for retry/requeue history.
-- scan_v1.sql remains frozen and readers never apply this change implicitly.

CREATE TABLE retry_attempts (
  attempt_id INTEGER PRIMARY KEY,
  attempt_uuid TEXT NOT NULL UNIQUE,
  operation TEXT NOT NULL CHECK (operation IN ('requeue','import_urls')),
  requested_at TEXT NOT NULL,
  where_json TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  backup_sha256 TEXT NOT NULL CHECK (length(backup_sha256) = 64),
  source_scan_uuid TEXT
);

CREATE TABLE retry_transitions (
  attempt_id INTEGER NOT NULL REFERENCES retry_attempts(attempt_id),
  url_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  from_frontier_state TEXT NOT NULL,
  to_frontier_state TEXT NOT NULL,
  prior_page_json TEXT,
  prior_document_id INTEGER,
  prior_evidence_sha256 TEXT,
  removed_links INTEGER NOT NULL CHECK (removed_links >= 0),
  removed_forms INTEGER NOT NULL CHECK (removed_forms >= 0),
  removed_resource_refs INTEGER NOT NULL CHECK (removed_resource_refs >= 0),
  removed_contexts INTEGER NOT NULL CHECK (removed_contexts >= 0),
  PRIMARY KEY (attempt_id, url_id)
);

CREATE INDEX retry_transitions_url_id ON retry_transitions(url_id, attempt_id);

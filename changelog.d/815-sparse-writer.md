- Reuse the exact corpus capability summary while a native metadata-only scan
  has no response, document, body, or resource evidence, avoiding repeated
  whole-corpus reads on every sparse page commit without changing scan storage.

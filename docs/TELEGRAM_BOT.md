# Guided bot conversation contract

The guided-bot roadmap (epic #751) adds a conversational way to configure,
confirm and follow a scan without a terminal. This document is the contract
that every delivery surface — starting with Telegram — binds to. The code
that executes it lives in `seohead/bot/`:

- `seohead/bot/contract.py` — the versioned state machine as data
  (`CONTRACT_VERSION = "seohead.bot.conversation/1"`, states, actions,
  transitions, editable fields).
- `seohead/bot/wizard.py` — `WizardSession`, the driver: turns events into
  `Reply` objects (text + button tokens), resolves the effective crawl
  configuration through `seohead.crawl.settings`, and hands confirmed jobs
  to a `JobSubmitter` protocol.

The package contains no Telegram SDK code, no network calls and no crawl
logic. The adapter owns platform identity, markup and delivery; the driver
owns the conversation; the shared core owns the scan.

## States

| State | What the user sees | Actions accepted |
|---|---|---|
| `awaiting_site` | "Send the site address to scan." | `answer`, `help`, `cancel` |
| `awaiting_project` | Project buttons + "new" | `answer`, `back`, `help`, `cancel` |
| `awaiting_policy` | Named scan policies (`quick`, `standard`, `thorough`) | `answer`, `back`, `help`, `cancel` |
| `awaiting_report` | Report format buttons (`xlsx`, `docx`, `csv`, `md`, `json`), optional `problems-only` suffix | `answer`, `back`, `help`, `cancel` |
| `preview` | The resolved effective settings + config fingerprint | `confirm`, `edit`, `back`, `help`, `cancel` |
| `confirming` | Explicit final approval ("press start") | `confirm`, `back`, `help`, `cancel` |
| `running` | Progress updates | `progress`, `finish` (worker), `cancel` |
| `done` | Completion + report handle | `rerun`, `help` |
| `cancelled` | Draft discarded | `rerun` (restart), `help` |

`describe_contract()` returns the same table as JSON for adapters that want
to render it programmatically.

## Transitions

Forward is `answer` through the four collecting states, `confirm` from
`preview` to `confirming`, `confirm` again to submit and enter `running`,
`finish` to `done`. `back` walks the collecting states in reverse and
re-renders `preview` when it lands there. `edit` from `preview` jumps into
one collecting state and returns to `preview` on the next valid `answer`.
`rerun` from `done` re-enters `preview` — a rerun is re-shown and
re-confirmed, never submitted sight unseen — and from `cancelled` it starts
a fresh draft.

Anything outside `allowed_actions(state)` is an invalid step: the driver
replies with a notice and stays. Invalid steps never advance and never
submit.

## Invariants

- **Preview is submission.** The effective configuration is resolved once,
  when `preview` is entered, via `crawl.settings.load()` — the same loader
  the CLI uses — and that same `config`, `manifest()` and `fingerprint()`
  triple is what `JobSubmitter.submit` receives. Confirmation cannot drift
  from what the user was shown.
- **Nothing starts on a bad step.** Invalid answers, disallowed actions and
  expired sessions produce a reply, stay, or reset — and never reach the
  submitter.
- **Expiry recovers loudly.** A session idle past its TTL resets to
  `awaiting_site` with an explicit notice; a lapsed `running` session also
  requests cancellation so no job keeps running unattended.
- **No UI handler implements crawl logic.** `JobSubmitter` is the only
  boundary: `submit(spec) -> job_id`, `cancel(job_id) -> bool`. Scan
  execution is the queue/core's job (issue #771 and its siblings).
- **Credentials stay out of chat.** The wizard accepts named policies and
  dotted setting paths resolved by the core loader; credential headers
  remain `env:` references resolved at request time by
  `settings.resolve_credential_headers`, and the preview shows the manifest
  (which redacts credential values) rather than the raw config.

## Versioning

`CONTRACT_VERSION` identifies the shape of a persisted conversation. Bump it
whenever a state, action, transition or field changes; adapters and session
stores compare it before resuming, and refuse — with a fresh start — a
conversation they do not understand.

## Synthetic coverage

`tests/test_bot_wizard.py` drives the whole flow offline — site → project →
policy → report → preview → confirm → running → done — plus invalid-answer
stays, back/edit round-trips, cancellation before and during a run, expiry
before and during a run, and rerun identity. `tests/test_bot_contract.py`
pins the transition table against `allowed_actions`.

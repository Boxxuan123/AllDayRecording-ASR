# AllDayRecording V3 contracts

This directory is the single source of truth for every Computer/Phone V3 wire
contract. Python, the Desktop Vue client, and the Phone ArkTS client may keep
generated decoders, but they must not redefine server enums or resource shapes.

## Versioning

- Contract version: `3.7.0`
- Mobile projection version: `4`
- JSON Schema dialect: 2020-12
- OpenAPI version: 3.1

`manifest.json` lists every public schema and API document with its SHA-256.
Contract tests reject unindexed files and stale digests. A compatible change
increments the contract patch/minor version. A breaking wire change requires a
new API path and contract major version.

## Consumer rules

- API producers emit only enum values declared in `schemas/primitives.schema.json`.
- Consumers map an enum value they do not know to their local `unknown` display
  state while retaining the raw value for diagnostics.
- IDs are UUIDv7 or ULID strings. Legacy numeric IDs stay inside `legacy_ref`.
- Timestamps are UTC ISO 8601 values ending in `Z`; capture timezone is a
  separate IANA timezone field.
- Public media references use `media_id`; absolute filesystem paths are never a
  wire field.
- `fixtures/core-resources.json` is the cross-language acceptance fixture.
  `fixtures/forward-enums.json` intentionally contains future enum values and is
  a consumer-compatibility fixture, not a producer-valid resource.
- `schemas/timeline-quality.schema.json` freezes the V3.1-C reviewed seam audit
  input and content-hashed release receipt. It is a Computer-side release gate,
  not a Phone projection resource.
- `schemas/knowledge.schema.json` freezes the V3.2 evidence, event, memory,
  generation-provenance, and proposal-review resources.
- `schemas/reminder.schema.json` freezes V3.3 reminder operations, generation
  submissions, accepted schedules, and the Phone projection. Only accepted
  schedules cross the device boundary; unconfirmed model candidates remain in
  the Desktop review queue. Projection `4` resets the Phone cursor so existing
  accepted reminders are replayed safely.
- `POST /api/v3/reminder-generations/codex` is the user-triggered Codex adapter.
  It sends transcript text but never audio or local paths, uses an empty
  read-only working directory with approvals denied, and keeps generated
  candidates behind the Desktop review boundary by default.
- `schemas/person.schema.json` freezes V3.4 open-set speaker clusters, explicit
  person links, representative clips, and reversible human operations. Unknown
  is a first-class Desktop result. V3.7 adds four-tier decisions, per-prototype
  human reviews, person maturity, hard negatives, opt-in automatic matching, and
  historical rematching. A cluster label never bulk-promotes voiceprints; only a
  confirmed individual prototype enters the stable library. These Desktop-only
  resources do not enter the Phone projection, which remains on projection `4`.
- `schemas/person-memory.schema.json` freezes V3.5 Desktop-only person profiles,
  cross-day interactions, typed memories, validity, confirmation state, event /
  utterance evidence, reminder links, and append-only correction responses.
  Model observations remain distinct from facts. No person-memory resource is
  added to the Phone projection; accepted reminders continue to cross through
  the existing projection `4` boundary.
- `schemas/insight.schema.json` freezes V3.6 Desktop-only daily summaries,
  objective statistics, relationship facts, evidence-bound model observations,
  immutable revisions, and correction operations. Summaries are regenerated
  from the current event layer rather than prior summaries. Insight resources
  do not enter the Phone projection, which remains on projection `4`.

The OpenAPI documents deliberately have disjoint paths and security schemes:
Desktop endpoints live under `/api/v3`, while paired-device endpoints live under
`/device/v3` and declare their required device scopes.

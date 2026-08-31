# AllDayRecording V3 contracts

This directory is the single source of truth for every Computer/Phone V3 wire
contract. Python, the Desktop Vue client, and the Phone ArkTS client may keep
generated decoders, but they must not redefine server enums or resource shapes.

## Versioning

- Contract version: `3.0.0`
- Mobile projection version: `1`
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

The OpenAPI documents deliberately have disjoint paths and security schemes:
Desktop endpoints live under `/api/v3`, while paired-device endpoints live under
`/device/v3` and declare their required device scopes.

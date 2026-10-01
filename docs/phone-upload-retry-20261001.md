# Phone upload retry compatibility

The receiver previously returned HTTP 409 when an already admitted V2 session
was submitted again with identical audio but refreshed completion evidence. A
watch stop record can arrive after manual completion, changing only the evidence
source or confirmation time. Reinstalling a client built from a checkout that
supports an older phone database schema also prevents synchronization before any
upload can proceed.

The receiver now accepts completion-only confirmation for both supported
manifest versions. Session identity, audio format, chunk positions, counts,
sample totals, continuity and admitted audio hashes must still match. It stores
the new evidence and a replayable receipt without replacing the original
manifest, advancing the audio input revision or changing processing/profile
inputs. Changed chunks and conflicting audio remain rejected.

Client installation must include the locally installed database migrations and
be performed as an update, preserving recordings, pairing and review data.

Validation: 35 device sync and transfer tests passed, including completion
source/time refresh, repeated confirmation after restart, unchanged input
revision and backup location, and existing conflicting-prefix/audio rejection.
The compatible phone build and six Blind Review V2 queue cases also passed.

Rollback: revert the receiver compatibility change; refreshed V2 completion
evidence will again be rejected with HTTP 409. Do not install a phone client
whose maximum supported database version is below the device database version.

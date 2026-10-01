# Durable phone sample reviews

Phone sample choices now persist locally before the review page advances. Each
choice has a stable operation ID that survives restart and transport retries.
The receiver acknowledges an already committed operation even if its original
review item is no longer pending. It rejects reuse for a different device,
prototype or decision.

Explicit human sample authorization and withdrawal support both named known
people and the self person. Known-person quality policy checks remain in place.
Self reviews do not create known-person policy revisions or trigger historical
known-person rematching. Frozen Blind snapshots and the separation between
learning, blind and holdout remain unchanged.

The phone database migration adds a durable voice review queue at version 17.
Client packages must support the installed database version; installing an older
client can prevent synchronization. A failed item retains its operation ID while
independent items can continue. For Blind/purity truth, later operations on the
same review wait for a failed predecessor, preserving submit/edit/undo order.

Validation covers real SQLite migration/restart, remote commit followed by local
write failure, same-operation retry, failure isolation, ordered undo, local save
failure, repeated taps, stale evidence, self authorization/withdrawal and
operation collision rejection. Blind V2 tests import their shared fixture
explicitly so targeted collection does not depend on test module order.

Historical private benchmark documents and one-off repair scripts are retained
locally and are not part of this delivery. No private report, recording,
embedding or device identifier is committed.

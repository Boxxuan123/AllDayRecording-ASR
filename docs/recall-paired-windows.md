# Paired native conversation/date windows

A supported two-clause recap keeps each explicitly named native group paired with its own calendar date. `scope.source_windows` is a disjunction of joint native namespace/date predicates. The enclosing `sent_from` / `sent_to` fields are a bounding envelope, not permission to read every room on every intervening date.

The deterministic parser uses the existing authorized native directory and fixed request clock. It accepts only unambiguous date-before-room assignments. Explicit source/person/date conflicts, unknown or duplicate native room identities, unsupported clock/daypart clauses, and unassociated dates remain unqueryable. An omitted second year can use the first clause's written year; it does not use message content or model guesses.

The existing investigator owns both clauses. Source requests, body records/bytes, cache scans, cancellation, deadline and model calls share the original query budgets; no new executor or message store exists. Message reads split into the intersected clause windows. Related-account reads stay inside those same windows. The native context API can return adjacent records, which are checked and filtered against the corresponding clause before selection or original-text display.

A named actor remains a semantic subject. Questions from the user and answers from other participants within the allowed groups are retained as context. Native sender labels generate scoped candidates; only coherent stable source-account evidence can resolve a query-local subject. Same labels never create persistent person links or cross-platform merges.

Each accepted record retains its own context scope. The outbound preflight checks all nested local/Unix windows and rejects records outside the joint hard scope. Existing authorization, revision/retraction checks, E/C handles and exact current-round citation validation remain active.

Run the synthetic production-entry regressions with:

```powershell
python -m pytest tests/test_recall_scope_clauses.py -q
python tools/quality/check_recall_stability.py --output <local-receipt-directory>
```

These protocol tests do not establish semantic answer quality. Real-model and device acceptance are separate release gates.

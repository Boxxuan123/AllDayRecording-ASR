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


## Request modifiers and bounded review

A source-native private contact followed by a grammatical conversation modifier keeps that contact as subject and preserves the modifier as semantic context. A full native label always wins; unknown or multiple prefixes cannot be shortened to one convenient known person. No identity link is persisted.

An adjacent calendar date and HH:MM identifies the entire written minute; HH:MM:SS identifies one second. The existing timezone, invalid-calendar, DST fold/gap and explicit filter checks remain. These message clock constraints cannot be expanded by an investigative discovery action. A clock occurring later in an event description is not silently converted to a message send-time point.

A short range endpoint can inherit the first written month and year. An invalid or earlier day is rejected rather than moved to another month. Approximate cross-midnight wording retains both calendar endpoints with an explicit search-coverage notice; it does not fabricate precise event endpoints or silently stop at the first midnight. Explicit query filters still intersect this coverage.

Reading evidence can use the existing 45-second per-call ceiling even before the final turn. Empty planning stays capped at25 seconds. Nonfinal evidence review reserves30 seconds for a later synthesis and2 for validation; late additional source requests are skipped to protect that reserve. Actual remaining query time always applies. The query ceiling remains90 seconds/four model calls; source, record, byte, prompt and retry limits are unchanged. Tests of a synthetic30-second completion verify the allocation, not real semantic success.

带普通传输前缀及单一合法 XML 声明的消息，在本地语义投影中保留当前正文和引用边界；原记录、发送者、修订及授权映射保持原样。畸形/重复声明、DOCTYPE、ENTITY 与附件访问参数仍拒绝或脱敏。跨午夜短日期使用明确披露的宽候选时段，不将检索边界声称为事件的确切时间。

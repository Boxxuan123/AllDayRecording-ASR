"""Keep exact event candidates without disabling hard constraints or broad keyword filters."""

from allday_asr.v3.domain.recall_event_anchor import literal_anchor
from allday_asr.v3.domain.recall_investigation_scope import inside_root


class InvestigationRelevance:
    def candidate_terms(self, terms):
        # A literal event name/identifier must also be present in the search terms.
        anchor = next(
            (
                a
                for a in self.anchors
                if any(
                    t.casefold() in a.casefold() or a.casefold() in t.casefold()
                    for t in terms
                )
            ),
            None,
        )
        return [anchor] if anchor else terms

    def topic_rows(self, rows, terms):
        kept = []
        invalid = self.service.sources.changed(rows)
        for row in rows:
            self.flow.record("candidate_seen", row, "scoped_topic_candidate")
            if row["id"] in invalid or not inside_root(
                row, self.root_scope, evidence=True
            ):
                self.flow.record("hard_rejected", row, "topic_scope_or_integrity")
                continue
            self.flow.record(
                "application_accepted", row, "hard_scope_and_integrity_valid"
            )
            candidates = self.candidate_terms(terms)
            if (
                candidates
                and candidates[0] in self.anchors
                and not literal_anchor(row["quote"], candidates)
            ):
                self.flow.record("soft_rejected", row, "different_literal_event")
            elif all(t.casefold() in row["quote"].casefold() for t in terms):
                kept.append(row)
            elif literal_anchor(row["quote"], self.anchors):
                row = {
                    **row,
                    "retrieval_basis": "literal_event_anchor_candidate",
                    "relation_ids": [],
                    "relevance_confirmed": False,
                }
                self.flow.record(
                    "soft_terms_not_matched", row, "kept_literal_event_anchor"
                )
                kept.append(row)
            else:
                self.flow.record(
                    "soft_rejected", row, "initial_keywords_without_relationship"
                )
        return kept

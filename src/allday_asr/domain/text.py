from __future__ import annotations

import unicodedata


def normalize_text(value: str) -> str:
    """Normalize text for the project's character-level comparisons."""
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def levenshtein_operations(reference: str, hypothesis: str) -> dict[str, int]:
    """Return the deterministic edit-operation breakdown used by evaluations."""
    previous = [(index, 0, 0, index) for index in range(len(hypothesis) + 1)]
    for ref_index, reference_character in enumerate(reference, start=1):
        current = [(ref_index, 0, ref_index, 0)]
        for hyp_index, hypothesis_character in enumerate(hypothesis, start=1):
            if reference_character == hypothesis_character:
                substitution = previous[hyp_index - 1]
            else:
                distance, substitutions, deletions, insertions = previous[
                    hyp_index - 1
                ]
                substitution = (
                    distance + 1,
                    substitutions + 1,
                    deletions,
                    insertions,
                )
            distance, substitutions, deletions, insertions = previous[hyp_index]
            deletion = (distance + 1, substitutions, deletions + 1, insertions)
            distance, substitutions, deletions, insertions = current[hyp_index - 1]
            insertion = (distance + 1, substitutions, deletions, insertions + 1)
            current.append(min(substitution, deletion, insertion))
        previous = current
    distance, substitutions, deletions, insertions = previous[-1]
    return {
        "distance": distance,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
    }

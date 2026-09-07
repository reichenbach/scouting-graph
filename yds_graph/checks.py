"""The fail-closed test on the model step.

The model writes prose. Code then proves, number by number, that the prose
says nothing the facts sheet did not already say. A single number that does
not trace back is enough to reject the draft.

Documented exception: a ball-strike count written in baseball notation
(0-0, 1-2, 3-2) is treated as notation, not as a claim.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from . import config


CITATION_RE = re.compile(r"\[(f\d+)\]")
COUNT_NOTATION_RE = re.compile(r"\b[0-3]-[0-2]\b")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Violation:
    pitcher: str
    kind: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    return parts


def numbers_in(text: str) -> list[float]:
    stripped = CITATION_RE.sub(" ", text)
    stripped = COUNT_NOTATION_RE.sub(" ", stripped)
    return [float(m) for m in NUMBER_RE.findall(stripped)]


def _matches(number: float, fact: dict) -> bool:
    if abs(number - float(fact["value"])) <= config.NUMBER_TOLERANCE:
        return True
    # A sample size is a legitimate thing to cite alongside a value.
    return abs(number - float(fact["n"])) < 0.001


def check_notes(pitcher: str, notes: str, facts_sheet: dict) -> list[Violation]:
    violations: list[Violation] = []
    facts = facts_sheet["facts"]
    allowed_ids = set(facts_sheet["pitchers"].get(pitcher, {}).get("fact_ids", []))

    if not notes or not notes.strip():
        return [Violation(pitcher, "empty", "The model returned no notes for this pitcher.")]

    sentences = split_sentences(notes)
    if not (config.MIN_SENTENCES <= len(sentences) <= config.MAX_SENTENCES):
        violations.append(
            Violation(
                pitcher,
                "sentence_count",
                f"{len(sentences)} sentences; the contract is "
                f"{config.MIN_SENTENCES} to {config.MAX_SENTENCES}.",
            )
        )

    for sentence in sentences:
        cited = CITATION_RE.findall(sentence)
        for fact_id in cited:
            if fact_id not in facts:
                violations.append(
                    Violation(pitcher, "unknown_fact_id", f"Cited [{fact_id}], which does not exist.")
                )
            elif fact_id not in allowed_ids:
                violations.append(
                    Violation(
                        pitcher,
                        "wrong_pitcher_fact",
                        f"Cited [{fact_id}], which belongs to another pitcher.",
                    )
                )

        nums = numbers_in(sentence)
        if not nums:
            continue
        cited_facts = [facts[i] for i in cited if i in facts]
        if not cited_facts:
            violations.append(
                Violation(
                    pitcher,
                    "uncited_number",
                    f"Sentence carries a number with no fact id: {sentence!r}",
                )
            )
            continue
        for num in nums:
            if not any(_matches(num, fact) for fact in cited_facts):
                violations.append(
                    Violation(
                        pitcher,
                        "number_not_in_facts",
                        f"The value {num} does not match any cited fact "
                        f"({', '.join(f['id'] + '=' + str(f['value']) for f in cited_facts)}) "
                        f"in: {sentence!r}",
                    )
                )
    return violations


def check_all_notes(draft_notes: dict[str, str], facts_sheet: dict) -> list[Violation]:
    violations: list[Violation] = []
    for pitcher in facts_sheet["pitchers"]:
        violations.extend(check_notes(pitcher, draft_notes.get(pitcher, ""), facts_sheet))
    extra = set(draft_notes) - set(facts_sheet["pitchers"])
    for pitcher in sorted(extra):
        violations.append(
            Violation(pitcher, "unknown_pitcher", "The model wrote notes for a pitcher not in the file.")
        )
    return violations


def check_answer_numbers(answer: str, tool_result_texts: list[str]) -> list[Violation]:
    """Assistant version: every number in the answer must be in a tool result."""
    grounded: list[float] = []
    for text in tool_result_texts:
        grounded.extend(numbers_in(text))

    violations: list[Violation] = []
    for num in numbers_in(answer):
        if not any(abs(num - g) <= config.NUMBER_TOLERANCE for g in grounded):
            violations.append(
                Violation(
                    "assistant",
                    "number_not_in_tool_results",
                    f"The value {num} does not appear in any tool result.",
                )
            )
    return violations

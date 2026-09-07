"""The only place in this repo that talks to Claude.

Two rules live here:

1. The model is handed a rendered facts sheet and nothing else about the data.
   It never sees the raw file.
2. Whatever comes back is treated as untrusted prose until checks.py has
   proved every number in it.

Set YDS_GRAPH_STUB=1 to run the whole pipeline with a deterministic stub and
no network. Set YDS_GRAPH_STUB=fabricate to run a stub that invents a number,
which is how the fail-closed path is demonstrated.
"""

from __future__ import annotations

import json

from . import config
from .facts import facts_for, render_fact_lines


NOTES_SYSTEM = """You write short scouting notes for a college baseball staff.

Hard rules, and the pipeline checks all of them in code after you answer:
- Write 4 to 7 sentences per pitcher. No more, no fewer.
- Use only numbers that appear in the facts sheet you were given. You may
  round to a whole number. You may not compute, combine, average, or infer
  any new number.
- Every sentence that contains a number must cite the fact it came from
  inline, in square brackets, like [f12]. Cite the fact for each number used.
- Only cite fact ids listed for that pitcher.
- No praise, no narrative, no projection about what a pitcher will do next.
  Describe what the sample shows and let the staff decide.
- Plain sentences. No markdown, no bullet points, no dashes for punctuation.

If a fact you would want does not exist, say nothing about it."""

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pitcher": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["pitcher", "notes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["notes"],
    "additionalProperties": False,
}


class ModelRefusal(RuntimeError):
    pass


def build_notes_prompt(facts_sheet: dict, feedback: str | None = None) -> str:
    blocks = []
    for pitcher in sorted(facts_sheet["pitchers"]):
        blocks.append(f"PITCHER: {pitcher}\n{render_fact_lines(facts_sheet, pitcher)}")
    prompt = (
        "Facts sheet, version "
        + facts_sheet["version"]
        + ", built from "
        + facts_sheet["provenance"]["source_file"]
        + ".\n\n"
        + "\n\n".join(blocks)
        + "\n\nWrite the notes for each pitcher above."
    )
    if feedback:
        prompt += (
            "\n\nYour previous draft was rejected by the automatic check for these reasons. "
            "Fix all of them:\n" + feedback
        )
    return prompt


# --------------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------------

def _pick_facts(facts_sheet: dict, pitcher: str) -> list[dict]:
    facts = facts_for(facts_sheet, pitcher)
    wanted_prefixes = ["usage.", "velocity.mean.", "first_pitch_strike_rate", "chase_rate", "whiff_rate."]
    chosen: list[dict] = []
    for prefix in wanted_prefixes:
        for fact in facts:
            if fact["metric"].startswith(prefix) and fact not in chosen:
                chosen.append(fact)
                break
    for fact in facts:
        if len(chosen) >= 5:
            break
        if fact not in chosen:
            chosen.append(fact)
    return chosen[:5]


def _sentence(fact: dict) -> str:
    unit = "percent" if fact["unit"] == "percent" else fact["unit"]
    return f"On {fact['label']}, the sample reads {fact['value']} {unit} [{fact['id']}]."


def stub_write_notes(facts_sheet: dict, feedback: str | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pitcher in sorted(facts_sheet["pitchers"]):
        chosen = _pick_facts(facts_sheet, pitcher)
        out[pitcher] = " ".join(_sentence(f) for f in chosen)
    return out


def stub_write_notes_fabricating(facts_sheet: dict, feedback: str | None = None) -> dict[str, str]:
    """A stub that invents a number. The post-check must catch this."""
    out: dict[str, str] = {}
    for pitcher in sorted(facts_sheet["pitchers"]):
        chosen = _pick_facts(facts_sheet, pitcher)
        anchor = chosen[0]
        invented = round(float(anchor["value"]) + 33.3, 1)
        sentences = [_sentence(f) for f in chosen[:4]]
        sentences.append(
            f"He also worked ahead in the count {invented} percent of the time [{anchor['id']}]."
        )
        out[pitcher] = " ".join(sentences)
    return out


# --------------------------------------------------------------------------
# The real call
# --------------------------------------------------------------------------

def _client():
    import anthropic

    config._load_env()
    return anthropic.Anthropic()


def _create(client, **kwargs):
    """Create a message, with server side refusal fallbacks when available."""
    import anthropic

    try:
        return client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **kwargs,
        )
    except (anthropic.BadRequestError, TypeError):
        # The beta is not enabled for this account, or the SDK does not know
        # the parameter. Fall back to a plain call rather than failing the run.
        return client.messages.create(**kwargs)


def live_write_notes(facts_sheet: dict, feedback: str | None = None) -> dict[str, str]:
    client = _client()
    response = _create(
        client,
        model=config.MODEL_ID,
        max_tokens=config.MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=NOTES_SYSTEM,
        messages=[{"role": "user", "content": build_notes_prompt(facts_sheet, feedback)}],
        output_config={"format": {"type": "json_schema", "schema": NOTES_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise ModelRefusal(f"Model declined the request: {response.stop_details}")
    text = next(b.text for b in response.content if b.type == "text")
    payload = json.loads(text)
    return {row["pitcher"]: row["notes"] for row in payload["notes"]}


def get_notes_writer():
    """Return the callable the graph uses. Tests monkeypatch this."""
    mode = config.stub_mode()
    if mode == "fabricate":
        return stub_write_notes_fabricating
    if mode:
        return stub_write_notes
    return live_write_notes

"""The only place in this repo that talks to Claude.

Two rules live here:

1. The model is handed a rendered facts sheet and nothing else about the data.
   It never sees the raw file.
2. Whatever comes back is treated as untrusted prose until checks.py has
   proved every number in it.

Claude is the default, and the hosted path in this file is unchanged. There
is a second path for any server that speaks the OpenAI chat API, which is what
llama.cpp's server and ollama expose, so the whole pipeline runs on a machine
on your own network with no key and no bill. See config.model_backend.

Set YDS_GRAPH_STUB=1 to run the whole pipeline with a deterministic stub and
no network. Set YDS_GRAPH_STUB=fabricate to run a stub that invents a number,
which is how the fail-closed path is demonstrated.
"""

from __future__ import annotations

import json
import re

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


# --------------------------------------------------------------------------
# The OpenAI compatible path: llama.cpp, ollama, anything that speaks /v1
# --------------------------------------------------------------------------

class ModelTransportError(RuntimeError):
    """The local server could not be reached or answered with an error."""


LOCAL_NOTES_SYSTEM = (
    NOTES_SYSTEM
    + """

Answer with one JSON object and nothing else. No prose before it, no prose
after it, no code fence. The object has exactly one key:

{"notes": "Sentence one [f1]. Sentence two [f2]. Sentence three [f3]. Sentence four [f4]."}

Copy every number exactly as it appears in the facts sheet, digit for digit.
Put the fact id in square brackets in the same sentence as the number it
belongs to.

Write exactly 5 sentences. Not 4, not 6, not 12. The contract allows 4 to 7,
and 5 is the safe middle, so aim at 5 and count them before you answer. Pick
the 5 facts a staff would look at first and ignore the rest; the whole facts
table is printed underneath your notes anyway. One number per sentence is
plenty.

Write the pitcher id exactly as it is given, as in OPP-11. Never spell it out,
never split it, and never write the digits from it on their own, because a
loose number in a sentence reads as a statistic and the check rejects it.

Every sentence ends with a period."""
)


def _local_notes_prompt(facts_sheet: dict, pitcher: str, feedback: str | None) -> str:
    """One pitcher per call. Small models hold one facts sheet, not four."""
    prompt = (
        "Facts sheet, version "
        + facts_sheet["version"]
        + ", built from "
        + facts_sheet["provenance"]["source_file"]
        + ".\n\nPITCHER: "
        + pitcher
        + "\n"
        + render_fact_lines(facts_sheet, pitcher)
        + "\n\nWrite the notes for "
        + pitcher
        + " as the JSON object described above."
    )
    if feedback:
        prompt += (
            "\n\nYour previous draft was rejected by the automatic check for these "
            "reasons. Fix all of them:\n" + feedback
        )
    return prompt


def extract_json_object(text: str) -> dict:
    """Pull the first balanced JSON object out of whatever the model said.

    A local model wraps its answer in a code fence, or apologises first, or
    adds a closing sentence. None of that is a reason to fail a run, so the
    object is located rather than assumed. What is inside it is still checked
    by checks.py like every other draft.
    """
    if text is None:
        raise ValueError("no text to parse")
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        loaded = json.loads(cleaned)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    try:
                        loaded = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(loaded, dict):
                        return loaded
                    break
        start = cleaned.find("{", start + 1)
    raise ValueError(f"no JSON object in model output: {text[:400]!r}")


def openai_chat(
    messages: list[dict],
    *,
    system: str | None = None,
    json_object: bool = False,
    max_tokens: int | None = None,
    temperature: float = 0.0,
) -> str:
    """One call to an OpenAI compatible /v1/chat/completions endpoint.

    Deliberately httpx and not the openai package: this is one POST, and the
    fewer pinned dependencies this repo carries the easier it is to run.
    """
    import httpx

    base_url = config.model_base_url()
    if not base_url:
        raise ModelTransportError(
            "YDS_MODEL_BASE_URL is not set. Point it at a local server, for "
            "example http://localhost:8080/v1."
        )
    model_name = config.model_name()
    if not model_name:
        raise ModelTransportError("YDS_MODEL_NAME is not set.")

    payload: dict = {
        "model": model_name,
        "messages": ([{"role": "system", "content": system}] if system else []) + messages,
        "temperature": temperature,
        "max_tokens": max_tokens or config.LOCAL_MAX_TOKENS,
        "stream": False,
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {config.model_api_key()}"}
    url = base_url + "/chat/completions"

    def post(body: dict):
        try:
            return httpx.post(
                url, json=body, headers=headers, timeout=config.LOCAL_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            raise ModelTransportError(f"could not reach {url}: {exc}") from exc

    response = post(payload)
    if response.status_code >= 400 and json_object:
        # Not every server implements response_format. Ask again without it;
        # the tolerant parser is what actually makes the JSON safe.
        payload.pop("response_format", None)
        response = post(payload)
    if response.status_code >= 400:
        raise ModelTransportError(
            f"{url} returned {response.status_code}: {response.text[:400]}"
        )
    try:
        data = response.json()
        return data["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelTransportError(
            f"unexpected response from {url}: {response.text[:400]}"
        ) from exc


def openai_write_notes(facts_sheet: dict, feedback: str | None = None) -> dict[str, str]:
    """The notes step against a local model, one pitcher at a time."""
    out: dict[str, str] = {}
    for pitcher in sorted(facts_sheet["pitchers"]):
        prompt = _local_notes_prompt(facts_sheet, pitcher, feedback)
        messages = [{"role": "user", "content": prompt}]
        text = openai_chat(messages, system=LOCAL_NOTES_SYSTEM, json_object=True)
        try:
            payload = extract_json_object(text)
            notes = payload["notes"]
        except (ValueError, KeyError, TypeError):
            # One retry, showing the model its own output. If it fails again
            # the empty string goes to checks.py, which holds the run.
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        'That was not the required shape. Answer again with one JSON '
                        'object and nothing else, exactly like '
                        '{"notes": "..."} where the value is the sentences.'
                    ),
                },
            ]
            text = openai_chat(messages, system=LOCAL_NOTES_SYSTEM, json_object=True)
            try:
                notes = extract_json_object(text)["notes"]
            except (ValueError, KeyError, TypeError):
                notes = ""
        if isinstance(notes, list):
            notes = " ".join(str(part) for part in notes)
        out[pitcher] = str(notes or "").strip()
    return out


LIBRARY_SYSTEM = """You answer a college baseball staff question from retrieved library passages.

Hard rules, checked in code after you answer:
- Use only the passages you were given. If they do not contain the answer, say you have nothing on file.
- Every sentence that contains a number must cite the passage id in square brackets, like [c3].
- Only cite ids from the passages listed. Do not invent a passage id.
- Do not compute, combine, average, or infer a new number. Copy a number as it appears.
- Plain sentences. No markdown, no bullet points, no dashes for punctuation.
- Do not answer roster, schedule, or opponent-stat questions from this library. Those are SQL. If the question is of that kind, say so and stop.

If the passages do not support an answer, say you have nothing on file and state no number."""


def build_library_prompt(
    question: str, passages: list[dict], feedback: str | None = None
) -> str:
    from .library import render_passages

    prompt = (
        "Question from the staff:\n"
        + question
        + "\n\nRetrieved passages:\n"
        + render_passages(passages)
        + "\n\nAnswer from those passages only."
    )
    if feedback:
        prompt += (
            "\n\nYour previous draft was rejected by the automatic check for these "
            "reasons. Fix all of them:\n" + feedback
        )
    return prompt


def stub_write_library(
    question: str, passages: list[dict], feedback: str | None = None
) -> str:
    if not passages:
        return "Nothing on file about that. The library does not have a passage for this question."
    sentences = []
    for passage in passages[:2]:
        first = passage["text"].split(".")[0].strip()
        if first and not first.endswith("."):
            first += "."
        sentences.append(f"{first} [{passage['id']}].")
    return " ".join(sentences)


def stub_write_library_fabricating(
    question: str, passages: list[dict], feedback: str | None = None
) -> str:
    base = stub_write_library(question, passages, feedback)
    return base + " The conference also caps a pitcher at 127 pitches in a weekend [c1]."


def live_write_library(
    question: str, passages: list[dict], feedback: str | None = None
) -> str:
    client = _client()
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    response = _create(
        client,
        model=config.MODEL_ID,
        max_tokens=2000,
        system=LIBRARY_SYSTEM,
        messages=[
            {"role": "user", "content": build_library_prompt(question, passages, feedback)}
        ],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    if response.stop_reason == "refusal":
        raise ModelRefusal(f"Model declined the request: {response.stop_details}")
    text = next(b.text for b in response.content if b.type == "text")
    return str(json.loads(text)["answer"]).strip()


LOCAL_LIBRARY_SYSTEM = (
    LIBRARY_SYSTEM
    + """

Answer with one JSON object and nothing else:
{"answer": "Sentence one [c1]. Sentence two [c2]."}

Copy numbers exactly as they appear in a cited passage. If nothing was retrieved, the object is {"answer": "Nothing on file about that."} and it contains no number."""
)


def openai_write_library(
    question: str, passages: list[dict], feedback: str | None = None
) -> str:
    prompt = build_library_prompt(question, passages, feedback)
    messages = [{"role": "user", "content": prompt}]
    text = openai_chat(messages, system=LOCAL_LIBRARY_SYSTEM, json_object=True)
    try:
        answer = extract_json_object(text)["answer"]
    except (ValueError, KeyError, TypeError):
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": (
                    'That was not the required shape. Answer again with one JSON '
                    'object exactly like {"answer": "..."}.'
                ),
            },
        ]
        text = openai_chat(messages, system=LOCAL_LIBRARY_SYSTEM, json_object=True)
        try:
            answer = extract_json_object(text)["answer"]
        except (ValueError, KeyError, TypeError):
            answer = ""
    return str(answer or "").strip()


def get_library_writer():
    """Return the callable the library graph uses. Tests monkeypatch this."""
    mode = config.stub_mode()
    if mode == "fabricate":
        return stub_write_library_fabricating
    if mode:
        return stub_write_library
    backend = config.model_backend()
    if backend == config.BACKEND_STUB:
        return stub_write_library
    if backend == config.BACKEND_OPENAI_COMPAT:
        return openai_write_library
    return live_write_library


def get_notes_writer():
    """Return the callable the graph uses. Tests monkeypatch this."""
    mode = config.stub_mode()
    if mode == "fabricate":
        return stub_write_notes_fabricating
    if mode:
        return stub_write_notes
    backend = config.model_backend()
    if backend == config.BACKEND_STUB:
        return stub_write_notes
    if backend == config.BACKEND_OPENAI_COMPAT:
        return openai_write_notes
    return live_write_notes

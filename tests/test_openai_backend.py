"""The local model path, proved offline.

There is no network in this file. A tiny HTTP server runs in a thread and
speaks the OpenAI chat schema, so the parser, the retry, the response_format
fallback and the tool loop are all exercised the way a real llama.cpp or
ollama server would exercise them.

What is proved here:

- a JSON notes payload wrapped in prose and a code fence is still read
- a first answer that is not JSON is retried exactly once
- a server that rejects response_format is asked again without it
- the tool loop calls a tool, feeds the result back, and takes the final
- an invented number from a local model is held, same as any other model
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from yds_graph import assistant_graph, config, model, tools


class _Server:
    """An OpenAI compatible endpoint that answers however the test says."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responder = None  # set by the test: (payload, index) -> str or dict
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server = server
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
                index = len(outer.requests)
                outer.requests.append(payload)
                answer = outer.responder(payload, index)

                if isinstance(answer, dict) and "status" in answer:
                    body = json.dumps({"error": answer.get("error", "no")}).encode()
                    self.send_response(answer["status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                body = json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "model": payload.get("model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": answer},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def last_user_message(self) -> str:
        for message in reversed(self.requests[-1]["messages"]):
            if message["role"] == "user":
                return message["content"]
        return ""

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stub_server(monkeypatch):
    server = _Server()
    monkeypatch.setenv("YDS_MODEL_BACKEND", "openai_compat")
    monkeypatch.setenv("YDS_MODEL_BASE_URL", server.base_url)
    monkeypatch.setenv("YDS_MODEL_NAME", "test-local-model")
    monkeypatch.delenv("YDS_MODEL_API_KEY", raising=False)
    try:
        yield server
    finally:
        server.close()


def _sheet(home):
    import pandas as pd

    from yds_graph.adapters import adapt
    from yds_graph.facts import compute_facts, file_provenance

    source = home / "inbox" / "sample.csv"
    frame, _ = adapt(pd.read_csv(source))
    return compute_facts(frame, file_provenance(source, frame))


def _good_notes(sheet, pitcher: str) -> str:
    facts = [sheet["facts"][i] for i in sheet["pitchers"][pitcher]["fact_ids"][:4]]
    return " ".join(
        f"The sample shows {f['label']} at {f['value']} [{f['id']}]." for f in facts
    )


def _pitcher_in(prompt: str, sheet) -> str:
    for pitcher in sheet["pitchers"]:
        if pitcher in prompt:
            return pitcher
    raise AssertionError(f"no known pitcher in prompt: {prompt[:200]}")


# --------------------------------------------------------------------------
# the notes step
# --------------------------------------------------------------------------

def test_notes_payload_is_read_out_of_prose_and_a_code_fence(home, stub_server):
    sheet = _sheet(home)

    def responder(payload, index):
        pitcher = _pitcher_in(payload["messages"][-1]["content"], sheet)
        body = json.dumps({"notes": _good_notes(sheet, pitcher)})
        return f"Sure, here you go.\n```json\n{body}\n```\nHope that helps."

    stub_server.responder = responder

    from yds_graph.checks import check_all_notes

    notes = model.openai_write_notes(sheet)
    assert set(notes) == set(sheet["pitchers"])
    assert check_all_notes(notes, sheet) == []
    # one call per pitcher, and the facts sheet was actually sent
    assert len(stub_server.requests) == len(sheet["pitchers"])
    assert stub_server.requests[0]["messages"][0]["role"] == "system"
    assert stub_server.requests[0]["response_format"] == {"type": "json_object"}


def test_unparseable_first_answer_is_retried_exactly_once(home, stub_server):
    sheet = _sheet(home)
    pitchers = sorted(sheet["pitchers"])

    def responder(payload, index):
        prompt = payload["messages"][1]["content"]
        pitcher = _pitcher_in(prompt, sheet)
        if pitcher == pitchers[0] and index == 0:
            return "I am not going to answer in JSON, sorry."
        return json.dumps({"notes": _good_notes(sheet, pitcher)})

    stub_server.responder = responder

    from yds_graph.checks import check_all_notes

    notes = model.openai_write_notes(sheet)
    assert check_all_notes(notes, sheet) == []
    # one extra call for the retry, and no more than one
    assert len(stub_server.requests) == len(pitchers) + 1
    assert stub_server.requests[1]["messages"][-1]["content"].startswith(
        "That was not the required shape"
    )


def test_two_bad_answers_leave_empty_notes_and_the_check_holds(home, stub_server):
    sheet = _sheet(home)
    stub_server.responder = lambda payload, index: "still not JSON"

    from yds_graph.checks import check_all_notes

    notes = model.openai_write_notes(sheet)
    assert set(notes.values()) == {""}
    violations = check_all_notes(notes, sheet)
    assert violations, "empty notes must not pass the check"
    assert all(v.kind == "empty" for v in violations)


def test_a_server_that_rejects_response_format_is_asked_again_without_it(home, stub_server):
    sheet = _sheet(home)

    def responder(payload, index):
        if "response_format" in payload:
            return {"status": 400, "error": "response_format is not supported"}
        pitcher = _pitcher_in(payload["messages"][-1]["content"], sheet)
        return json.dumps({"notes": _good_notes(sheet, pitcher)})

    stub_server.responder = responder

    from yds_graph.checks import check_all_notes

    notes = model.openai_write_notes(sheet)
    assert check_all_notes(notes, sheet) == []
    assert "response_format" in stub_server.requests[0]
    assert "response_format" not in stub_server.requests[1]


def test_an_unreachable_server_raises_a_transport_error(home, monkeypatch):
    monkeypatch.setenv("YDS_MODEL_BACKEND", "openai_compat")
    monkeypatch.setenv("YDS_MODEL_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("YDS_MODEL_NAME", "nothing-is-listening")
    with pytest.raises(model.ModelTransportError):
        model.openai_chat([{"role": "user", "content": "hello"}])


# --------------------------------------------------------------------------
# the tool loop
# --------------------------------------------------------------------------

def test_tool_loop_calls_a_tool_then_takes_the_final(home, stub_server):
    tools.build_db()

    def responder(payload, index):
        if index == 0:
            return json.dumps(
                {
                    "tool": "query_roster",
                    "args": {
                        "sql": "SELECT player, positions_played FROM roster "
                        "WHERE positions_played LIKE '%SS%'"
                    },
                }
            )
        return json.dumps(
            {"final": "On file, these players have taken reps at short: Cedar and Oak."}
        )

    stub_server.responder = responder

    result = assistant_graph._openai_agent("Who can play short?", [], None)
    assert len(stub_server.requests) == 2
    assert [c["name"] for c in result["tool_calls"]] == ["query_roster"]
    assert result["answer"].startswith("On file")
    # the tool result was fed back to the model
    assert stub_server.requests[1]["messages"][-1]["content"].startswith(
        "TOOL RESULT (query_roster)"
    )


def test_tool_loop_recovers_from_one_unparseable_turn(home, stub_server):
    tools.build_db()

    def responder(payload, index):
        if index == 0:
            return "Let me think about that for a moment."
        return json.dumps({"final": "Nothing on file for that."})

    stub_server.responder = responder

    result = assistant_graph._openai_agent("Anything on file?", [], None)
    assert result["answer"] == "Nothing on file for that."
    assert result["tool_calls"] == []
    assert "not a valid action" in stub_server.requests[1]["messages"][-1]["content"]


def test_tool_loop_that_never_finals_refuses_rather_than_inventing(home, stub_server):
    tools.build_db()
    stub_server.responder = lambda payload, index: "no action at all"

    result = assistant_graph._openai_agent("Who is next?", [], None)
    assert len(stub_server.requests) == assistant_graph.MAX_LOCAL_STEPS
    assert "not stating one" in result["answer"]
    assert result["tool_calls"] == []


def test_a_local_model_that_invents_a_number_is_held(home, stub_server, monkeypatch):
    """Same fail closed path as the hosted model, through the whole graph."""
    monkeypatch.delenv("YDS_GRAPH_STUB", raising=False)
    tools.build_db()

    def responder(payload, index):
        if index in (0, 2):
            return json.dumps(
                {
                    "tool": "query_schedule",
                    "args": {"sql": "SELECT date, opponent FROM schedule ORDER BY date LIMIT 1"},
                }
            )
        return json.dumps(
            {"final": "He throws his slider 91.7 percent of the time in two strike counts."}
        )

    stub_server.responder = responder

    result = assistant_graph.ask("What about the slider?", "ask-local-fabricate")
    assert result["status"] == "held"
    assert "cannot trace" in result["answer"]


def test_the_local_backend_is_chosen_without_being_named(home, stub_server, monkeypatch):
    """Auto detect: no key plus a base URL means the local server."""
    monkeypatch.delenv("YDS_GRAPH_STUB", raising=False)
    monkeypatch.delenv("YDS_MODEL_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "_load_env", lambda: None)

    assert config.model_backend() == config.BACKEND_OPENAI_COMPAT
    assert model.get_notes_writer() is model.openai_write_notes
    assert assistant_graph.get_agent() is assistant_graph._openai_agent


def test_a_placeholder_key_counts_as_no_key(home, monkeypatch):
    monkeypatch.delenv("YDS_GRAPH_STUB", raising=False)
    monkeypatch.setenv("YDS_MODEL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.delenv("YDS_MODEL_BACKEND", raising=False)
    for placeholder in ("sk-ant-...", "sk-ant-PASTE_YOUR_KEY_HERE"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", placeholder)
        config._load_env()
        assert not config.api_key_available()
        assert config.model_backend() == config.BACKEND_OPENAI_COMPAT


def test_the_loop_reads_the_action_shapes_a_small_model_actually_emits():
    """Same intent, different wrapper. The tool still has to be a real one."""
    parse = assistant_graph._parse_action

    named = parse('{"tool": "query_roster", "args": {"sql": "SELECT 1"}}')
    assert named == {"kind": "tool", "name": "query_roster", "args": {"sql": "SELECT 1"}}
    # the shape llama3.2 emitted: the tool name is the only key
    assert parse('{"query_roster": {"sql": "SELECT 1"}}') == named
    assert parse('{"name": "query_roster", "arguments": {"sql": "SELECT 1"}}') == named
    assert parse('{"final": "done."}') == {"kind": "final", "text": "done."}
    assert parse('{"answer": "done."}') == {"kind": "final", "text": "done."}

    with pytest.raises(ValueError):
        parse('{"rm_rf": {"path": "/"}}')
    with pytest.raises(ValueError):
        parse('{"tool": "delete_everything", "args": {}}')
    with pytest.raises(ValueError):
        parse("no action here at all")

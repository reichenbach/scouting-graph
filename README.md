# yds_graph

A working model of how a scouting report gets built when a model is in the
loop, and a small staff assistant built on the same rules.

Two LangGraph graphs, one doctrine. The report graph turns a pitch tracking
export into a one page per pitcher PDF, and refuses to produce one when the
data or the prose does not hold up. The assistant graph answers staff
questions over a small SQL database and refuses to state a number it cannot
trace.

Everything in this repo is synthetic. No real coach, school, program, player
or vendor appears anywhere in it.

## The doctrine, in five bullets

1. Numbers come from deterministic code. `facts.py` computes every number a
   report can contain. The model never sees the raw file and never computes.
2. The model writes prose from a computed facts sheet, and nothing else. Each
   number in the prose carries a fact id.
3. Gates block and never guess. Schema, coverage and sanity are checked in
   code. A failure holds the whole report; it does not degrade it.
4. A person approves before delivery. The graph pauses at a real interrupt.
   Nothing reaches the outbox without a human decision on the record.
5. Every run writes an audit row with a version stamp and data provenance,
   including the runs that stop early. Errors are loud to the operator and
   kind to the coach.

The sixth rule, which is really the first: the check on the model is code, not
instructions. The prompt asks for cited numbers. `checks.py` then proves it,
number by number, and a single number that does not trace back holds the run.

## The report graph

```mermaid
flowchart TD
    START([start]) --> ingest
    ingest[ingest<br/>adapter maps export headers] --> validate
    validate[validate<br/>schema, coverage, sanity gates]
    validate -->|any gate failed| hold
    validate -->|clean| compute_facts
    compute_facts[compute_facts<br/>pure pandas, every number gets an id and an n] --> write_notes
    write_notes[write_notes<br/>Claude writes prose from the facts sheet]
    write_notes -->|numbers do not check out, first time| write_notes
    write_notes -->|numbers do not check out, second time| hold
    write_notes -->|every number traces| review
    review[review<br/>interrupt, a person decides]
    review -->|reject| hold
    review -->|approve| render_pdf
    render_pdf[render_pdf<br/>facts table, notes, provenance footer] --> deliver
    deliver[deliver<br/>outbox plus audit row] --> DONE([end])
    hold[hold<br/>hold note plus audit row] --> DONE
```

## The assistant graph

```mermaid
flowchart TD
    START([question]) --> agent
    agent[agent<br/>tool calls: roster SQL, schedule SQL, opponent facts, draft message] --> post_check
    post_check[post_check<br/>every number must appear in a tool result]
    post_check -->|number not grounded, first time| agent
    post_check -->|number not grounded, second time| hold_answer
    post_check -->|message to parents, players or staff| review
    post_check -->|clean| persist
    review[review<br/>interrupt, never sent] --> persist
    persist[persist<br/>audit row; note stored only if the retention flag allows] --> DONE([end])
    hold_answer[hold_answer<br/>refuses to answer, audit row] --> DONE
```

## Running it

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/make_sample_data.py      # seeded synthetic data
cp .env.example .env                              # then put a key in it
```

Everything below runs against the real model. Prefix any command with
`YDS_GRAPH_STUB=1` to run the entire pipeline with a deterministic stub and no
network, which is how the tests run.

```bash
# a clean file: pauses for review
.venv/bin/python -m yds_graph run inbox/sample.csv --coach sample

# resume from that thread id, from this shell or any other
.venv/bin/python -m yds_graph resume <thread_id> --approve
.venv/bin/python -m yds_graph resume <thread_id> --reject "velocity looks off in game two"

# a file that should never produce a report
.venv/bin/python -m yds_graph run sample_data/bad_schema.csv --coach sample
.venv/bin/python -m yds_graph run sample_data/thin_coverage.csv --coach sample

# the staff assistant
.venv/bin/python -m yds_graph ask "What do we have on the Friday matchup?"
.venv/bin/python -m yds_graph ask "Follow up on that" --session <session_id>

# what happened, and to which file
.venv/bin/python -m yds_graph audit
```

`make test`, `make run-sample` and `make ask` wrap the common ones.

### Resuming

The graphs use a SQLite checkpointer at `state/checkpoints.sqlite`, so a run
that paused in one process resumes in another by thread id. `run` prints the
thread id and the exact resume commands. There is a test that proves this
across two real subprocesses.

## Three worked examples

**Friday matchup.**

```
$ python -m yds_graph ask "What do we have on the Friday matchup?"
Here is what is on file for the next series. Schedule rows on file:
[{"date": "2026-09-11", "opponent": "Opponent A", "site": "home", "travel": "none"}, ...]
For OPP-11, the facts sheet reads: [f1] Fastball usage overall: 54.2% (n=308)
[f2] Slider usage overall: 25.0% (n=308). Those are the only numbers on file
for that pitcher.

session=ask-c82a84c771 status=done retention=store stored=True
```

Every number in that answer came back from `query_schedule` or
`opponent_facts`, and the post-check proved it before the answer was printed.

**Shortstop out.**

```
$ python -m yds_graph ask "Our shortstop is out this weekend, who can play there?"
On file, these players have taken reps at SS: Cedar, Dogwood, Fir, Larch, Oak,
Spruce. That is what the roster records, not a depth chart. The staff decides
the order. Availability detail stays with the staff and is not written down
here.

session=ask-60e56345de status=done retention=do_not_store stored=False
```

The answer is a roster query, not a recommendation. The question tripped the
retention flag, so the note was not written to the database. The audit row
records that it was answered and that nothing was stored.

**Parent message.**

```
$ python -m yds_graph ask "Draft a message to parents about the weekend series."
I put together a draft for parents. Nothing has been sent and nothing will be
sent by me. It is waiting for a coach to read it.

========================================================================
PAUSED FOR HUMAN REVIEW
========================================================================
{ "kind": "message_review", "drafts": ["kind=parents\nstatus=NEEDS HUMAN REVIEW..."] }

  approve: python -m yds_graph resume ask-9887767da5 --approve
```

The graph stops. There is no send path in this program at all, and approving
records that a person read it rather than delivering anything.

## What the tests prove

`make test` runs the suite with no network at all.

- A bad schema export stops at `validate`. Nothing reaches the outbox, a hold
  note exists with both a coach paragraph and an operator paragraph, and the
  audit row says HOLD with the gate code in the reason.
- A thin coverage export stops at `validate` for too few games and too few
  pitches.
- A fake model that invents a number is caught by the post-check, retried
  exactly once, and then held. The delivery path never runs.
- Rejecting at review delivers nothing and writes the reason into the hold
  note. Approving produces a PDF whose bytes contain the version stamp, the
  source file and its hash.
- A run paused in one process resumes in another, through the checkpointer.
- Assistant: a parent message request ends at an interrupt rather than a send.
  A numeric answer traces every number to a tool result. An injury question is
  not written to the database. An invented number holds the answer.
- The SQL tools refuse anything that is not a single SELECT on their own table.

One test is marked `live` and runs the real model end to end, including the
same post-check on real output. It skips unless `ANTHROPIC_API_KEY` is set:

```bash
.venv/bin/python -m pytest -m live
```

## Honest claim

LangGraph on a side project; production agents at work are custom Python and
MCP. This repo exists to make the rules visible and testable in a form
somebody else can run, not to claim a framework.

## Layout

```
yds_graph/
  adapters.py          export headers to one canonical schema
  gates.py             schema, coverage and sanity gates
  facts.py             every number, with an id, an n and a provenance block
  checks.py            the post-check that proves the prose
  model.py             the only file that talks to Claude, plus the stubs
  render.py            the PDF, with the provenance footer
  audit.py             the audit table
  report_graph.py      part one
  assistant_graph.py   part two
  tools.py             the assistant's SQL tools and database
scripts/make_sample_data.py
sample_data/           synthetic exports, roster, schedule, philosophy
tests/
```

## Model

`claude-opus-5`, adaptive thinking, structured output for the notes step, a
manual tool loop for the assistant. Pinned dependency versions are in
`requirements.txt`.

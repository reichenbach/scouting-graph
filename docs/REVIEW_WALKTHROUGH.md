# Human review walkthrough: interrupt, approve, audit

This is the recruiter-followable demo for doctrine items 4 and 5: a real
LangGraph interrupt, a human decision, and an audit row. Synthetic data only.
No API key. No Slack. Copy-paste the commands.

Done check from the job ramp: someone else can follow this page without help.

## Setup (once)

Python 3.11 or newer. `python3 --version` prints it. On Debian or Ubuntu, `python3 -m venv` needs a package that is not always installed. If the command says ensurepip is not available, install it and run the command again:

```bash
sudo apt install python3-venv
```

This walkthrough does not use `.env`.

```bash
git clone https://github.com/reichenbach/scouting-graph.git
cd scouting-graph
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/make_sample_data.py
```

`YDS_GRAPH_STUB=1` uses a deterministic stub model so the pipeline runs offline.

## 1. Interrupt

```bash
YDS_GRAPH_STUB=1 .venv/bin/python -m yds_graph run inbox/sample.csv --coach sample
```

The graph ingests the CSV, runs gates, builds the facts sheet, drafts notes,
then **pauses**. Nothing is written to `outbox/` yet. You should see:

```text
PAUSED FOR HUMAN REVIEW
...
thread_id: report-<hex>
  approve: YDS_GRAPH_STUB=1 .venv/bin/python -m yds_graph resume report-<hex> --approve
  reject : YDS_GRAPH_STUB=1 .venv/bin/python -m yds_graph resume report-<hex> --reject "reason"
```

Paste the `approve` line. It uses the venv interpreter and keeps the stub
flag, so the pasted command stays offline. The checkpointer at
`state/checkpoints.sqlite` holds the paused graph, so you can resume from a
different shell (or tomorrow).

## 2. Approve

```bash
YDS_GRAPH_STUB=1 .venv/bin/python -m yds_graph resume report-<hex> --approve
```

Expected:

```text
DELIVERED: .../outbox/sample/sample_report-<hex>.pdf
```

To refuse delivery instead:

```bash
YDS_GRAPH_STUB=1 .venv/bin/python -m yds_graph resume report-<hex> --reject "velocity looks off in game two"
```

A reject writes a hold note under `errors/` and an audit row with outcome
`HELD`. No PDF in the outbox.

## 3. Audit row

```bash
.venv/bin/python -m yds_graph audit
```

After an approve you should see a recent `report` row with outcome
`DELIVERED`, coach `sample`, source `sample.csv`, and version
`yds-graph 0.2.0`. After a reject, outcome is `HELD` and the reason column
carries what you typed.

One-liner for the newest few rows:

```bash
.venv/bin/python -m yds_graph audit --limit 5
```

## What this proves in an interview

- The stop is an interrupt in the graph, not a polite prompt.
- Delivery is gated on a recorded human decision.
- Holds and deliveries both leave an audit trail with provenance.

The numeric post-check and the golden CI suite prove the model cannot invent
a number. This walkthrough proves a person still has to say yes before the
PDF leaves.

## Makefile shortcut

```bash
make review-demo
```

Runs interrupt, approve, and audit with the stub model and prints the latest
audit rows.

## Without a terminal

YDS Review runs this same loop. Download it from a GitHub Release, or from the artifacts on the YDS Review workflow. Setup and the unsigned-app prompts are in the Desktop app section of the README. Stub mode is the default there too. No API key.

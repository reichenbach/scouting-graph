"""Command line entry point.

    python -m yds_graph run inbox/sample.csv --coach sample
    python -m yds_graph resume <thread_id> --approve
    python -m yds_graph resume <thread_id> --reject "velocity looks off in game two"
    python -m yds_graph ask "Our shortstop is out this weekend, who can play there?"
    python -m yds_graph audit
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import audit as audit_module
from . import assistant_graph, config, report_graph, tools


def _new_thread(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _print_interrupt(result: dict) -> bool:
    payload = result.get("__interrupt__")
    if not payload:
        return False
    value = payload[0].value
    print()
    print("=" * 72)
    print("PAUSED FOR HUMAN REVIEW")
    print("=" * 72)
    print(json.dumps(value, indent=2, default=str))
    return True


def cmd_run(args: argparse.Namespace) -> int:
    thread_id = args.thread or _new_thread("report")
    result = report_graph.start_run(args.csv, args.coach, thread_id)
    status = result.get("status")

    if _print_interrupt(result):
        print()
        print(f"thread_id: {thread_id}")
        print(f"  approve: python -m yds_graph resume {thread_id} --approve")
        print(f"  reject : python -m yds_graph resume {thread_id} --reject \"reason\"")
        return 0

    if status == "held":
        print(f"HELD. thread_id={thread_id}")
        print(f"Hold note: {result.get('hold_note_path')}")
        note = Path(result["hold_note_path"]).read_text()
        print()
        print(note)
        return 2

    print(f"status={status} thread_id={thread_id}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    if args.approve and args.reject:
        print("Choose one of --approve or --reject.", file=sys.stderr)
        return 64
    if not args.approve and args.reject is None:
        print("Pass --approve or --reject \"reason\".", file=sys.stderr)
        return 64

    approved = bool(args.approve)
    reason = args.reject or ""

    graph = report_graph.build_graph(report_graph.open_checkpointer())
    cfg = {"configurable": {"thread_id": args.thread_id}}
    snapshot = graph.get_state(cfg)
    values = snapshot.values or {}
    if not values:
        print(f"No state for thread {args.thread_id}.", file=sys.stderr)
        return 66

    if "source_path" in values:
        result = report_graph.resume_run(args.thread_id, approved, reason)
        status = result.get("status")
        if status == "delivered":
            print(f"DELIVERED: {result['outbox_path']}")
        elif status == "held":
            print(f"HELD: {result.get('hold_note_path')}")
        else:
            print(f"status={status}")
        return 0

    result = assistant_graph.resume(args.thread_id, approved, reason)
    print(f"status={result.get('status')} persisted={result.get('persisted')}")
    print("Nothing was sent. This program does not send messages.")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    session_id = args.session or _new_thread("ask")
    result = assistant_graph.ask(args.question, session_id)

    print()
    print(result.get("answer", ""))
    print()
    if _print_interrupt(result):
        print()
        print(f"session: {session_id}")
        print(f"  approve: python -m yds_graph resume {session_id} --approve")
        print(f"  reject : python -m yds_graph resume {session_id} --reject \"reason\"")
        return 0

    print(
        f"session={session_id} status={result.get('status')} "
        f"retention={result.get('retention')} stored={result.get('persisted')}"
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    rows = audit_module.read_rows(limit=args.limit)
    print(audit_module.format_table(rows))
    return 0


def cmd_init_data(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(config.home() / "scripts"))
    import make_sample_data

    make_sample_data.main()
    path = tools.build_db()
    print(f"assistant database at {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yds_graph", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the report pipeline on one CSV")
    run.add_argument("csv")
    run.add_argument("--coach", default="sample")
    run.add_argument("--thread", default=None)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="resume a paused run")
    resume.add_argument("thread_id")
    resume.add_argument("--approve", action="store_true")
    resume.add_argument("--reject", nargs="?", const="", default=None)
    resume.set_defaults(func=cmd_resume)

    ask = sub.add_parser("ask", help="ask the staff assistant")
    ask.add_argument("question")
    ask.add_argument("--session", default=None)
    ask.set_defaults(func=cmd_ask)

    aud = sub.add_parser("audit", help="print the audit table")
    aud.add_argument("--limit", type=int, default=50)
    aud.set_defaults(func=cmd_audit)

    init = sub.add_parser("init-data", help="regenerate the synthetic sample data")
    init.set_defaults(func=cmd_init_data)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

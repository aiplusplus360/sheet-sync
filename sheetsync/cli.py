"""Command line interface for sheet-sync."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .adapters import CsvStore, JsonFileStore, MemoryStore
from .changelog import Journal
from .engine import SyncPlan, apply_plan, diff_side, plan_sync
from .merge import POLICIES

EXIT_CLEAN = 0
EXIT_APPLIED = 1
EXIT_ERROR = 2
EXIT_CONFLICT = 3

KIND_SYMBOLS = {
    "added": "+",
    "modified": "~",
    "removed": "-",
    "resurrected": "^",
    "conflict": "!",
}


def _store(path: Optional[str]):
    if not path:
        return MemoryStore()
    return CsvStore(path) if path.endswith(".csv") else JsonFileStore(path)


def _load_rows(path: Optional[str]) -> dict:
    if not path:
        return {}
    return JsonFileStore(path).load()


def _require(path: str) -> str:
    """A path the user typed is a filename, not a store that may not exist yet."""
    if not Path(path).exists():
        raise FileNotFoundError(2, "No such file", path)
    return path


def _print_plan(plan: SyncPlan, policy: str, width: int) -> None:
    conflicts = [result for result in plan.conflicts]
    changes = [result for result in plan.applicable if result.kind != "unchanged"]
    counts = plan.counts
    if not changes and not conflicts:
        print(f"policy: {policy}   nothing to do ({len(plan.results)} row(s) in sync)")
        return
    summary = "  ".join(f"{key} {value}" for key, value in counts.items() if value)
    print(f"policy: {policy}   {summary}")
    if conflicts:
        print()
        print(f"{len(conflicts)} row(s) held back:")
        for result in conflicts:
            for conflict in result.conflicts:
                if conflict.unresolved:
                    field = "row deleted" if conflict.field == "__row__" else conflict.field
                    print(
                        f"  ! {result.row_id} {field}: "
                        f"base={conflict.base!r} left={conflict.left!r} right={conflict.right!r}"
                    )
    if changes:
        print()
        for result in changes:
            symbol = KIND_SYMBOLS.get(result.kind, "?")
            fields = ", ".join(change.field for change in result.changes)
            detail = f"  ({fields})" if fields else ""
            print(f"  {symbol} {result.row_id} {result.kind}{detail}")
    resolved = [result for result in plan.results if result.conflicts and not result.unresolved]
    if resolved:
        print()
        for result in resolved:
            for conflict in result.conflicts:
                print(
                    f"  {result.row_id} {conflict.field}: took {conflict.resolution!r} "
                    f"via {conflict.resolved_by} (dropped left={conflict.left!r} "
                    f"right={conflict.right!r})"
                )


def _print_conflicts_json(plan: SyncPlan) -> None:
    print(json.dumps({"conflicts": [r.to_dict() for r in plan.conflicts]}, indent=2, ensure_ascii=False))


def cmd_sync(args: argparse.Namespace) -> int:
    base = _store(args.base)
    left = _store(_require(args.left))
    right = _store(_require(args.right))

    plan = plan_sync(base.load(), left.load(), right.load(), policy=args.policy)

    if args.dry_run:
        if args.json:
            print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
        else:
            _print_plan(plan, args.policy, args.width)
        return EXIT_CONFLICT if plan.conflicts else EXIT_CLEAN

    journal = Journal(args.journal) if args.journal else None
    report = apply_plan(plan, [left, right], journal=journal, now=args.now)

    if args.update_base:
        rows = dict(base.load())
        for result in plan.applicable:
            rows[result.row_id] = result.merged
        base.save(rows)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_plan(plan, args.policy, args.width)
        print()
        print(f"written: {len(report.written)} row(s)")
        if args.update_base:
            print("base snapshot updated")

    if plan.conflicts:
        return EXIT_CONFLICT
    return EXIT_APPLIED if report.written else EXIT_CLEAN


def cmd_diff(args: argparse.Namespace) -> int:
    base = _load_rows(args.base)
    side = _load_rows(_require(args.side))
    changes = diff_side(base, side)
    if args.json:
        print(json.dumps({"changes": changes}, indent=2, ensure_ascii=False))
        return EXIT_CLEAN
    print(f"{len(changes)} changed row(s) in {args.side}")
    for change in changes:
        symbol = KIND_SYMBOLS.get(change["kind"], "?")
        print(f"  {symbol} {change['row']} {change['kind']}")
        for field in change["fields"]:
            print(f"      {field['field']}: {field['before']!r} -> {field['after']!r}")
    return EXIT_CLEAN


def cmd_log(args: argparse.Namespace) -> int:
    journal = Journal(args.journal)
    entries = journal.history(args.row) if args.row else journal.entries()
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return EXIT_CLEAN
    if not entries:
        print("journal is empty")
        return EXIT_CLEAN
    for entry in entries:
        applied = "applied" if entry.get("applied") else "held"
        print(f"{entry.get('ts')}  {entry.get('row')}  {entry.get('kind')}  ({applied})")
        for field in entry.get("fields", []):
            print(f"    {field['field']}: {field['before']!r} -> {field['after']!r}")
        for conflict in entry.get("conflicts", []):
            state = conflict.get("resolved_by", "unresolved")
            print(f"    ! {conflict['field']} ({state})")
    return EXIT_CLEAN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sheetsync",
        description="Two-way sync with three-way merge instead of last-write-wins.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_cmd = sub.add_parser("sync", help="merge two stores, optionally apply")
    sync_cmd.add_argument("--base", default=None, help="JSON snapshot both sides last agreed on")
    sync_cmd.add_argument("--left", required=True, help="left store (JSON or .csv)")
    sync_cmd.add_argument("--right", required=True, help="right store (JSON or .csv)")
    sync_cmd.add_argument("--policy", choices=list(POLICIES), default="manual")
    sync_cmd.add_argument("--journal", default=None, help="append decisions to this JSONL file")
    sync_cmd.add_argument("--dry-run", action="store_true")
    sync_cmd.add_argument(
        "--update-base",
        action="store_true",
        help="re-baseline --base to the merged result so the next run is a no-op",
    )
    sync_cmd.add_argument("--now", default=None, help="timestamp for journal entries (reproducible runs)")
    sync_cmd.add_argument("--width", type=int, default=110)
    sync_cmd.add_argument("--json", action="store_true")
    sync_cmd.set_defaults(func=cmd_sync)

    diff_cmd = sub.add_parser("diff", help="what one store changed since the base")
    diff_cmd.add_argument("--base", default=None)
    diff_cmd.add_argument("--side", required=True)
    diff_cmd.add_argument("--json", action="store_true")
    diff_cmd.set_defaults(func=cmd_diff)

    log_cmd = sub.add_parser("log", help="read the journal")
    log_cmd.add_argument("--journal", required=True)
    log_cmd.add_argument("--row", default=None)
    log_cmd.add_argument("--json", action="store_true")
    log_cmd.set_defaults(func=cmd_log)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: no such file: {exc.filename}", file=sys.stderr)
        return EXIT_ERROR
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

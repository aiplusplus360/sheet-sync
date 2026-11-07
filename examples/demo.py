"""Demo: two systems edited the same table at the same time. Merge them.

    python examples/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sheetsync import CsvStore, Journal, JsonFileStore, MemoryStore, sync  # noqa: E402
from sheetsync.cli import _print_plan  # noqa: E402
from sheetsync.engine import plan_sync  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def load(name: str) -> MemoryStore:
    return MemoryStore(JsonFileStore(DATA / name).load(), name=name)


def main() -> int:
    base, postgres, sheets = load("base.json"), load("postgres.json"), load("sheets.json")

    print("the same customer table, edited on both sides since the last sync")
    print("-" * 78)
    for label, store in (("postgres", postgres), ("sheets", sheets)):
        print(f"  {label}")
        for row in sorted(store.load().values(), key=lambda item: item.id):
            state = "deleted" if row.deleted else json.dumps(dict(sorted(row.fields.items())))
            print(f"    {row.id}  {state}")
    print()

    print("1. plan with the default policy (manual). nothing is written yet.")
    print("-" * 78)
    plan = plan_sync(base.load(), postgres.load(), sheets.load())
    _print_plan(plan, "manual", 100)
    print()

    print("2. apply it, with a journal")
    print("-" * 78)
    journal = Journal(Path(__file__).resolve().parent / "out" / "sync-log.jsonl")
    report = sync(base, postgres, sheets, journal=journal, now="2026-11-07T12:00:00+00:00", update_base=base)
    print(f"  written: {', '.join(report.written) or 'nothing'}")
    print(f"  held back: {', '.join(r.row_id for r in report.conflicts) or 'nothing'}")
    print()
    print("  the conflict is still both values, not a guess:")
    for result in report.conflicts:
        for conflict in result.conflicts:
            print(f"    {result.row_id}.{conflict.field}: base={conflict.base} "
                  f"postgres={conflict.left} sheets={conflict.right}")
    print()

    print("3. re-run after applying: the rows that merged are now in sync")
    print("-" * 78)
    again = sync(base, postgres, sheets, update_base=base)
    print(f"  written: {', '.join(again.written) or 'nothing'}")
    print(f"  still held: {', '.join(r.row_id for r in again.conflicts) or 'nothing'}")
    print()

    print("4. resolve the remaining conflict with a policy, and keep the evidence")
    print("-" * 78)
    resolved = sync(base, postgres, sheets, policy="prefer-newest", update_base=base,
                    journal=journal, now="2026-11-07T12:05:00+00:00")
    for result in resolved.plan.results:
        for conflict in result.conflicts:
            print(f"  {result.row_id}.{conflict.field}: took {conflict.resolution!r} "
                  f"via {conflict.resolved_by}")
            print(f"    conflict was postgres={conflict.left} sheets={conflict.right}")
    print(f"  written: {', '.join(resolved.written)}")
    print()

    print("5. re-baselined: a third run has nothing to do")
    print("-" * 78)
    settled = sync(base, postgres, sheets, update_base=base)
    print(f"  written: {', '.join(settled.written) or 'nothing'}")
    print(f"  rows considered: {len(settled.plan.results)}")
    print()

    print("6. the journal is the audit trail")
    print("-" * 78)
    for entry in journal.entries():
        fields = ", ".join(f"{f['field']} {f['before']!r}->{f['after']!r}" for f in entry["fields"])
        state = "applied" if entry["applied"] else "held"
        print(f"  {entry['ts'][11:16]}  {entry['row']}  {entry['kind']:<10} ({state})  {fields}")
    print()

    print("7. the same rows, written to a spreadsheet-shaped CSV")
    print("-" * 78)
    csv_path = Path(__file__).resolve().parent / "out" / "customers.csv"
    CsvStore(csv_path).save(postgres.load())
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    print("  " + lines[0])
    for line in lines[1:3]:
        print("  " + line)
    print()
    print("  CSV cells are text: '99' and 99 are different. The adapter does not guess.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

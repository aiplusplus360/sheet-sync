"""Plan a sync, then apply it. Nothing is written while planning.

``plan_sync`` is pure: three snapshots in, a list of decisions out. That matters
because a merge you cannot inspect before it runs is a merge you cannot trust
with a customer table. ``apply_plan`` is the only function that touches a store,
and it writes only rows the plan marked applicable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

from .merge import MergeResult, merge_many
from .model import Row

__all__ = ["SyncPlan", "SyncReport", "apply_plan", "diff_side", "plan_sync", "sync"]


@dataclass(frozen=True)
class SyncPlan:
    """Every merge decision for a run, in row-id order."""

    results: Tuple[MergeResult, ...] = ()
    policy: str = "manual"

    @property
    def applicable(self) -> Tuple[MergeResult, ...]:
        return tuple(result for result in self.results if result.applicable)

    @property
    def conflicts(self) -> Tuple[MergeResult, ...]:
        return tuple(result for result in self.results if result.unresolved)

    @property
    def resolved(self) -> Tuple[MergeResult, ...]:
        return tuple(
            result for result in self.results if result.conflicts and not result.unresolved
        )

    @property
    def changes(self) -> Tuple[MergeResult, ...]:
        return tuple(result for result in self.applicable if result.kind != "unchanged")

    @property
    def counts(self) -> dict:
        counts = {"added": 0, "modified": 0, "removed": 0, "resurrected": 0, "unchanged": 0, "held": 0}
        for result in self.results:
            if result.unresolved:
                counts["held"] += 1  # nothing written for this row
            else:
                counts[result.kind] = counts.get(result.kind, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "policy": self.policy,
            "counts": self.counts,
            "rows": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class SyncReport:
    """The plan plus what actually reached the stores."""

    plan: SyncPlan
    written: Tuple[str, ...] = ()

    @property
    def conflicts(self) -> Tuple[MergeResult, ...]:
        return self.plan.conflicts

    @property
    def changed(self) -> bool:
        return bool(self.written)

    def to_dict(self) -> dict:
        payload = self.plan.to_dict()
        payload["written"] = list(self.written)
        return payload


def plan_sync(
    base: Mapping[str, Row],
    left: Mapping[str, Row],
    right: Mapping[str, Row],
    *,
    policy: str = "manual",
) -> SyncPlan:
    """Merge three snapshots without touching any store.

    >>> left = {"1": Row("1", {"a": 1})}
    >>> right = {"1": Row("1", {"a": 2})}
    >>> plan = plan_sync({}, left, right)
    >>> len(plan.conflicts), len(plan.changes)
    (1, 0)
    """
    return SyncPlan(tuple(merge_many(base, left, right, policy=policy)), policy=policy)


def diff_side(base: Mapping[str, Row], side: Mapping[str, Row]) -> List[dict]:
    """What one store changed relative to the base snapshot."""
    from .model import row_diff

    payload = []
    for row_id in sorted(set(base) | set(side)):
        diff = row_diff(row_id, base.get(row_id), side.get(row_id))
        if diff.kind != "unchanged":
            payload.append(diff.to_dict())
    return payload


def apply_plan(
    plan: SyncPlan,
    stores: Sequence,
    *,
    journal=None,
    now: Optional[str] = None,
) -> SyncReport:
    """Write every applicable result into each store.

    Stores are duck-typed: anything with ``load() -> dict[str, Row]`` and
    ``save(rows)``. Each store is loaded once, updated in memory, saved once.
    """
    pending = {result.row_id: result for result in plan.applicable if result.kind != "unchanged"}

    written: List[str] = []
    if pending:
        for store in stores:
            rows = dict(store.load())
            for row_id, result in pending.items():
                assert result.merged is not None
                rows[row_id] = result.merged
            store.save(rows)
        written = sorted(pending)

    if journal is not None:
        for result in plan.results:
            if result.kind == "unchanged" and not result.conflicts:
                continue
            journal.record(
                {
                    "row": result.row_id,
                    "kind": result.kind,
                    "applied": result.applicable and result.kind != "unchanged",
                    "fields": [change.to_dict() for change in result.changes],
                    "conflicts": [conflict.to_dict() for conflict in result.conflicts],
                },
                now=now,
            )

    return SyncReport(plan=plan, written=tuple(written))


def sync(
    base_store,
    left_store,
    right_store,
    *,
    policy: str = "manual",
    journal=None,
    now: Optional[str] = None,
    update_base=None,
) -> SyncReport:
    """Plan against the base snapshot, apply to both sides, optionally re-baseline.

    ``update_base`` is what makes a second run a no-op: without it the base is
    stale and the same edits would be re-merged (correctly, but pointlessly).
    """
    plan = plan_sync(base_store.load(), left_store.load(), right_store.load(), policy=policy)
    report = apply_plan(plan, [left_store, right_store], journal=journal, now=now)

    if update_base is not None:
        rows = dict(update_base.load())
        for result in plan.applicable:
            assert result.merged is not None
            rows[result.row_id] = result.merged
        update_base.save(rows)

    return report

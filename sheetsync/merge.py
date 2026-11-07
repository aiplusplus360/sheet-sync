"""Three-way merge, and why "last write wins" is not one.

Both sides carry a base snapshot: what each store believed the row looked like
the last time they agreed. For every field, three cases exist:

* only one side differs from base -- that side edited it, take it;
* both differ, to the same value -- they agree, take it, say so;
* both differ, to different values -- *that is the conflict*, and no ordering of
  timestamps can recover the edit that gets dropped.

The last case is why this module refuses to guess by default. ``manual`` leaves
the row untouched and reports it; ``prefer-left``/``prefer-right``/
``prefer-newest`` pin a rule, apply it, and still record what was discarded.
A merge that cannot tell you what it overwrote is a data-loss engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .model import FieldChange, Row, diff_fields, same

__all__ = [
    "POLICIES",
    "PRESENCE_FIELD",
    "Conflict",
    "MergeResult",
    "merge_many",
    "merge_rows",
]

POLICIES = ("manual", "prefer-left", "prefer-right", "prefer-newest")

#: Field name used for conflicts about the row existing at all (delete vs edit).
PRESENCE_FIELD = "__row__"


@dataclass(frozen=True)
class Conflict:
    """Two sides changed the same cell to different values.

    ``resolution`` is the value that was written, and ``resolved_by`` names the
    rule that chose it. Both are ``None`` when the conflict is left for a human,
    which is the default.
    """

    row_id: str
    field: str
    base: Any
    left: Any
    right: Any
    kind: str = "value"  # value | delete_edit
    resolution: Any = None
    resolved_by: Optional[str] = None

    @property
    def unresolved(self) -> bool:
        return self.resolved_by is None

    def to_dict(self) -> dict:
        payload = {
            "row": self.row_id,
            "field": self.field,
            "kind": self.kind,
            "base": self.base,
            "left": self.left,
            "right": self.right,
            "resolved": not self.unresolved,
        }
        if not self.unresolved:
            payload["resolution"] = self.resolution
            payload["resolved_by"] = self.resolved_by
        return payload


@dataclass(frozen=True)
class MergeResult:
    """The merged row, plus everything the merge had to decide."""

    row_id: str
    kind: str  # unchanged | added | modified | removed | resurrected | conflict
    merged: Optional[Row]
    conflicts: Tuple[Conflict, ...] = ()
    changes: Tuple[FieldChange, ...] = ()

    @property
    def unresolved(self) -> bool:
        return any(conflict.unresolved for conflict in self.conflicts)

    @property
    def applicable(self) -> bool:
        """False for a row held back by an unresolved conflict."""
        return self.merged is not None and not self.unresolved

    @property
    def resolved_conflicts(self) -> Tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if not c.unresolved)

    def to_dict(self) -> dict:
        return {
            "row": self.row_id,
            "kind": self.kind,
            "applied": self.applicable,
            "fields": [change.to_dict() for change in self.changes],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


def _value(row: Optional[Row], name: str) -> Any:
    return None if row is None else row.get(name)


def _deleted(row: Optional[Row]) -> bool:
    return bool(row.deleted) if row is not None else False


def _pick(
    kind: str,
    row_id: str,
    field: str,
    base: Any,
    left: Any,
    right: Any,
    policy: str,
    left_row: Optional[Row],
    right_row: Optional[Row],
) -> Tuple[Any, Optional[str]]:
    """Apply a policy to one conflict. Returns (value, rule) or (None, None).

    Returning ``(None, None)`` means "no rule applies, leave it alone", which is
    the correct answer for ``manual`` and for ``prefer-newest`` with no usable
    timestamps. Guessing a tie-break here is how silent data loss starts.
    """
    if policy == "prefer-left":
        return left, "policy:prefer-left"
    if policy == "prefer-right":
        return right, "policy:prefer-right"
    if policy == "prefer-newest":
        left_at = left_row.modified_at if left_row is not None else None
        right_at = right_row.modified_at if right_row is not None else None
        if left_at and right_at and left_at != right_at:
            winner = "policy:prefer-newest"
            return (left, winner) if left_at > right_at else (right, winner)
    return None, None


def _merge_fields(
    row_id: str,
    base: Optional[Row],
    left: Row,
    right: Row,
    policy: str,
) -> Tuple[Dict[str, Any], Dict[str, int], List[Conflict]]:
    merged_fields: Dict[str, Any] = {}
    versions: Dict[str, int] = {}
    conflicts: List[Conflict] = []

    names = set(base.field_names() if base else ())
    names |= set(left.field_names()) | set(right.field_names())
    names |= set(left.versions) | set(right.versions)

    for name in sorted(names):
        base_value = _value(base, name)
        left_value = _value(left, name)
        right_value = _value(right, name)

        left_changed = not same(left_value, base_value)
        right_changed = not same(right_value, base_value)

        if left_changed and right_changed and not same(left_value, right_value):
            value, rule = _pick(
                "value", row_id, name, base_value, left_value, right_value, policy, left, right
            )
            conflict = Conflict(
                row_id=row_id,
                field=name,
                base=base_value,
                left=left_value,
                right=right_value,
                resolution=value if rule else None,
                resolved_by=rule,
            )
            conflicts.append(conflict)
            if not rule:
                # Keep the base value so the row is still coherent, but never
                # write it: an unresolved row is not applicable.
                value = base_value
            versions[name] = max(left.version(name), right.version(name)) + (1 if rule else 0)
        elif left_changed:
            value = left_value
            versions[name] = max(left.version(name), right.version(name))
        elif right_changed:
            value = right_value
            versions[name] = max(left.version(name), right.version(name))
        else:
            value = base_value
            versions[name] = max(left.version(name), right.version(name))

        if value is not None:
            merged_fields[name] = value

    return merged_fields, versions, conflicts


def merge_rows(
    row_id: str,
    base: Optional[Row],
    left: Optional[Row],
    right: Optional[Row],
    *,
    policy: str = "manual",
) -> MergeResult:
    """Merge one row from two sides relative to their shared base.

    >>> base = Row("1", {"price": 10, "name": "Widget"})
    >>> left = Row("1", {"price": 12, "name": "Widget"}, {"price": 1})
    >>> right = Row("1", {"price": 10, "name": "Widget"}, {"name": 1})
    >>> result = merge_rows("1", base, left, right)
    >>> result.kind, result.merged.get("price")
    ('modified', 12)

    >>> clash = merge_rows(
    ...     "1",
    ...     base,
    ...     Row("1", {"price": 12, "name": "Widget"}),
    ...     Row("1", {"price": 15, "name": "Widget"}),
    ... )
    >>> clash.kind, clash.applicable, len(clash.conflicts)
    ('conflict', False, 1)
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {', '.join(POLICIES)}")

    if base is None and left is None and right is None:
        return MergeResult(row_id, "unchanged", None)
    if left is None and right is None:
        # Nothing to copy: absence is not a deletion, tombstones are.
        return MergeResult(row_id, "unchanged", None)
    if left is None or right is None:
        return _adopt(row_id, base, left if right is None else right)

    base_deleted = _deleted(base)
    left_deleted = left.deleted
    right_deleted = right.deleted

    left_moved = left_deleted != base_deleted
    right_moved = right_deleted != base_deleted
    left_edits = () if left_deleted else diff_fields(row_id, base, left)
    right_edits = () if right_deleted else diff_fields(row_id, base, right)

    conflicts: List[Conflict] = []
    merged_deleted = base_deleted
    survivor: Optional[Row] = None

    def presence_conflict() -> Tuple[Any, Optional[str]]:
        value, rule = _pick(
            "delete_edit",
            row_id,
            PRESENCE_FIELD,
            base_deleted,
            left_deleted,
            right_deleted,
            policy,
            left,
            right,
        )
        conflicts.append(
            Conflict(
                row_id=row_id,
                field=PRESENCE_FIELD,
                base=base_deleted,
                left=left_deleted,
                right=right_deleted,
                kind="delete_edit",
                resolution=value if rule else None,
                resolved_by=rule,
            )
        )
        return value, rule

    if left_moved and right_moved:
        if left_deleted == right_deleted:
            merged_deleted = left_deleted
        else:
            # One side deleted the row while the other revived it: a real
            # disagreement, decided by policy or not at all.
            value, rule = presence_conflict()
            merged_deleted = bool(value) if rule else left_deleted
            if rule and not merged_deleted:
                survivor = right if left_deleted else left
    elif left_moved:
        merged_deleted = left_deleted
        if left_deleted and right_edits:
            # A delete is not more recent than an edit; it is a disagreement.
            value, rule = presence_conflict()
            merged_deleted = True if not rule else bool(value)
            if rule and not merged_deleted:
                survivor = right
    elif right_moved:
        merged_deleted = right_deleted
        if right_deleted and left_edits:
            value, rule = presence_conflict()
            merged_deleted = True if not rule else bool(value)
            if rule and not merged_deleted:
                survivor = left

    if merged_deleted:
        merged = Row(
            id=row_id,
            fields={},
            versions={
                name: max(left.version(name), right.version(name))
                for name in sorted(set(left.versions) | set(right.versions))
            },
            deleted=True,
            modified_at=left.modified_at or right.modified_at,
        )
        kind = "conflict" if any(c.unresolved for c in conflicts) else (
            "unchanged" if base_deleted else "removed"
        )
        return MergeResult(row_id, kind, merged, tuple(conflicts))

    if survivor is not None:
        # The edit beat the delete, so the row is exactly what the editor had.
        # Field-by-field merging against a tombstone would invent conflicts
        # between a deleted row's missing cells and the live side's values.
        changes = diff_fields(row_id, base if not base_deleted else None, survivor)
        kind = "conflict" if any(c.unresolved for c in conflicts) else (
            "resurrected" if base_deleted else ("modified" if changes else "unchanged")
        )
        return MergeResult(row_id, kind, survivor, tuple(conflicts), changes)

    merged_fields, versions, field_conflicts = _merge_fields(row_id, base, left, right, policy)
    conflicts.extend(field_conflicts)

    merged = Row(
        id=row_id,
        fields=merged_fields,
        versions=versions,
        deleted=False,
        modified_at=left.modified_at or right.modified_at,
    )
    changes = diff_fields(row_id, base if not base_deleted else None, merged)

    if any(conflict.unresolved for conflict in conflicts):
        kind = "conflict"
    elif base is None:
        kind = "added"
    elif base_deleted:
        kind = "resurrected"
    elif changes:
        kind = "modified"
    else:
        kind = "unchanged"

    return MergeResult(row_id, kind, merged, tuple(conflicts), changes)


def _adopt(row_id: str, base: Optional[Row], side: Row) -> MergeResult:
    """One side has the row, the other has never seen it."""
    if side.deleted:
        kind = "unchanged" if _deleted(base) else "removed"
        return MergeResult(row_id, kind, side.tombstone())
    if base is None:
        return MergeResult(row_id, "added", side, (), diff_fields(row_id, None, side))
    kind = "resurrected" if _deleted(base) else "modified"
    changes = diff_fields(row_id, base, side)
    return MergeResult(row_id, kind if changes or kind == "resurrected" else "unchanged", side, (), changes)


def merge_many(
    base: Mapping[str, Row],
    left: Mapping[str, Row],
    right: Mapping[str, Row],
    *,
    policy: str = "manual",
) -> List[MergeResult]:
    """Merge every row id known to any side, in id order.

    >>> left = {"1": Row("1", {"a": 1})}
    >>> right = {"1": Row("1", {"a": 2})}
    >>> results = merge_many({}, left, right)
    >>> results[0].kind
    'conflict'
    """
    ids: Sequence[str] = sorted(set(base) | set(left) | set(right))
    return [
        merge_rows(row_id, base.get(row_id), left.get(row_id), right.get(row_id), policy=policy)
        for row_id in ids
    ]

"""Rows, values and the comparison rules that make them comparable.

A sync engine lives or dies on one question: *are these two cells the same?*
Python's ``==`` is the wrong answer. ``True == 1`` is true, ``1 == 1.0`` is true
but ``"1" != 1``, and ``nan != nan`` would report every snapshot as changed.
So values are compared through a canonical form that keeps the distinctions a
spreadsheet makes (numbers, text, booleans, blanks) and drops the ones it does
not (``1`` and ``1.0`` are the same cell).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "Row",
    "canonical",
    "diff_fields",
    "row_diff",
    "sort_rows",
]


def canonical(value: Any) -> Tuple[Any, ...]:
    """Comparison form for a cell value.

    >>> canonical(1) == canonical(1.0)
    True
    >>> canonical(True) == canonical(1)
    False
    >>> canonical("1") == canonical(1)
    False
    >>> canonical(None) == canonical("")
    False
    """
    if value is None:
        return ("blank",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"non-finite number is not a cell value: {value!r}")
        return ("num", int(number)) if number.is_integer() else ("num", number)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(canonical(item) for item in value))
    if isinstance(value, Mapping):
        return ("map", tuple(sorted((str(k), canonical(v)) for k, v in value.items())))
    return ("repr", repr(value))


def same(left: Any, right: Any) -> bool:
    """Cell equality through the canonical form."""
    return canonical(left) == canonical(right)


@dataclass(frozen=True)
class Row:
    """One record. ``fields`` never stores ``None``; a blank cell is absent.

    ``versions`` counts how many times each field changed, which is what lets a
    later merge distinguish "I edited this" from "you edited this" without a
    wall clock. ``modified_at`` is optional and only used by ``prefer-newest``.
    """

    id: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    versions: Mapping[str, int] = field(default_factory=dict)
    deleted: bool = False
    modified_at: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("row id must not be empty")
        cleaned = {str(key): value for key, value in self.fields.items() if value is not None}
        object.__setattr__(self, "fields", cleaned)
        object.__setattr__(self, "versions", dict(self.versions))

    def get(self, name: str) -> Any:
        return self.fields.get(name)

    def version(self, name: str) -> int:
        return int(self.versions.get(name, 0))

    def field_names(self) -> Tuple[str, ...]:
        return tuple(sorted(self.fields))

    def bump(self, names: Iterable[str], modified_at: Optional[str] = None) -> "Row":
        """Copy with the given fields' versions incremented."""
        versions = dict(self.versions)
        for name in names:
            versions[name] = self.version(name) + 1
        return Row(
            id=self.id,
            fields=dict(self.fields),
            versions=versions,
            deleted=self.deleted,
            modified_at=modified_at if modified_at is not None else self.modified_at,
        )

    def tombstone(self) -> "Row":
        """A deleted row keeps its id and versions so the delete can propagate."""
        return Row(
            id=self.id,
            fields={},
            versions=dict(self.versions),
            deleted=True,
            modified_at=self.modified_at,
        )

    def to_dict(self) -> dict:
        payload = {"id": self.id, "deleted": self.deleted, "fields": dict(self.fields)}
        if self.versions:
            payload["versions"] = {key: int(value) for key, value in sorted(self.versions.items())}
        if self.modified_at:
            payload["modified_at"] = self.modified_at
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Row":
        row_id = payload.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise ValueError(f"row is missing a string id: {payload!r}")
        fields = payload.get("fields") or {}
        versions = payload.get("versions") or {}
        if not isinstance(fields, Mapping) or not isinstance(versions, Mapping):
            raise ValueError(f"row {row_id!r} has malformed fields or versions")
        return cls(
            id=row_id,
            fields=dict(fields),
            versions={str(k): int(v) for k, v in versions.items()},
            deleted=bool(payload.get("deleted", False)),
            modified_at=payload.get("modified_at"),
        )


@dataclass(frozen=True)
class FieldChange:
    """A field whose value differs from the base snapshot."""

    row_id: str
    field: str
    before: Any
    after: Any

    def to_dict(self) -> dict:
        return {"row": self.row_id, "field": self.field, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class RowDiff:
    """How one row differs between a base snapshot and one side."""

    row_id: str
    kind: str  # added | modified | removed | resurrected | unchanged
    changes: Tuple[FieldChange, ...] = ()

    def to_dict(self) -> dict:
        return {
            "row": self.row_id,
            "kind": self.kind,
            "fields": [change.to_dict() for change in self.changes],
        }


def diff_fields(row_id: str, base: Optional[Row], side: Optional[Row]) -> Tuple[FieldChange, ...]:
    """Field-level changes from ``base`` to ``side``, ordered by field name."""
    base_fields = base.fields if base is not None else {}
    side_fields = side.fields if side is not None else {}
    changes = []
    for name in sorted(set(base_fields) | set(side_fields)):
        before = base_fields.get(name)
        after = side_fields.get(name)
        if not same(before, after):
            changes.append(FieldChange(row_id=row_id, field=name, before=before, after=after))
    return tuple(changes)


def row_diff(row_id: str, base: Optional[Row], side: Optional[Row]) -> RowDiff:
    """Classify a row's change relative to the base snapshot.

    >>> base = Row("1", {"name": "Ada"})
    >>> row_diff("1", base, Row("1", {"name": "Ada"})).kind
    'unchanged'
    >>> row_diff("1", base, Row("1", {"name": "Grace"})).kind
    'modified'
    >>> row_diff("2", None, Row("2", {"name": "Grace"})).kind
    'added'
    """
    changes = diff_fields(row_id, base, side)
    if base is None and side is None:
        return RowDiff(row_id, "unchanged")
    if base is None:
        return RowDiff(row_id, "added", changes)
    if side is None:
        return RowDiff(row_id, "removed")
    if base.deleted and not side.deleted:
        return RowDiff(row_id, "resurrected", changes)
    if not base.deleted and side.deleted:
        return RowDiff(row_id, "removed")
    if not changes:
        return RowDiff(row_id, "unchanged")
    return RowDiff(row_id, "modified", changes)


def sort_rows(rows: Iterable[Row]) -> Dict[str, Row]:
    """Id-indexed rows, in id order, so every output of this package is stable."""
    return {row.id: row for row in sorted(rows, key=lambda item: item.id)}

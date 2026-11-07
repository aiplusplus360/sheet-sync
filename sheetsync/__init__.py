"""sheet-sync: two-way sync with three-way merge instead of last-write-wins.

>>> from sheetsync import Row, merge_rows
>>> base = Row("1", {"price": 10})
>>> left = Row("1", {"price": 12})
>>> right = Row("1", {"price": 10})
>>> merge_rows("1", base, left, right).merged.get("price")
12
"""

from .adapters import CsvStore, JsonFileStore, MemoryStore, Store
from .changelog import Journal, utcnow
from .engine import SyncPlan, SyncReport, apply_plan, diff_side, plan_sync, sync
from .merge import (
    POLICIES,
    PRESENCE_FIELD,
    Conflict,
    MergeResult,
    merge_many,
    merge_rows,
)
from .model import FieldChange, Row, RowDiff, canonical, diff_fields, row_diff, same

__all__ = [
    "CsvStore",
    "Conflict",
    "FieldChange",
    "JsonFileStore",
    "Journal",
    "MemoryStore",
    "MergeResult",
    "POLICIES",
    "PRESENCE_FIELD",
    "Row",
    "RowDiff",
    "Store",
    "SyncPlan",
    "SyncReport",
    "apply_plan",
    "canonical",
    "diff_fields",
    "diff_side",
    "merge_many",
    "merge_rows",
    "plan_sync",
    "row_diff",
    "same",
    "sync",
    "utcnow",
]

__version__ = "1.0.0"

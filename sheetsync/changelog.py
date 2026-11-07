"""Append-only journal of every decision a sync made.

A merge report tells you what happened *this run*. The journal tells you what
happened to a row six weeks ago, which is the question anyone actually asks when
a value looks wrong. One JSON object per line, so it appends without rewriting
and can be tailed, grepped or shipped to a log store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

__all__ = ["Journal", "utcnow"]


def utcnow() -> str:
    """Second-resolution UTC timestamp, the format the journal stores."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Journal:
    """JSONL append log. Nothing is buffered, so a crash keeps the record."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or utcnow

    def record(self, entry: Dict, *, now: Optional[str] = None) -> Dict:
        """Append one entry, stamped. Returns exactly what was written."""
        payload = dict(entry)
        payload["ts"] = now or self.clock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return payload

    def entries(self) -> List[Dict]:
        """Every entry, oldest first. A malformed line is reported, not skipped."""
        if not self.path.exists():
            return []
        out: List[Dict] = []
        with self.path.open("r", encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{self.path}:{number} is not valid JSON: {exc}") from exc
        return out

    def rows_touched(self) -> List[str]:
        """Distinct row ids in first-seen order."""
        seen: Dict[str, None] = {}
        for entry in self.entries():
            row = entry.get("row")
            if isinstance(row, str):
                seen.setdefault(row, None)
        return list(seen)

    def history(self, row_id: str) -> List[Dict]:
        return [entry for entry in self.entries() if entry.get("row") == row_id]

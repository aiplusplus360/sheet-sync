"""Storage adapters.

A store is anything with ``load() -> dict[str, Row]`` and ``save(rows)``. That is
the whole interface, because the alternative -- a framework with a connection
pool, a session and a repository base class -- is how a 400-line sync engine
becomes a 40,000-line platform.

Postgres, Airtable or the Google Sheets API each take about twenty lines on top
of this: load rows into ``Row`` objects, ``save`` the merged ones back. The
adapters here cover the cases that need no credentials, which is also what the
test suite needs.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Union

from .model import Row, sort_rows

__all__ = ["CsvStore", "JsonFileStore", "MemoryStore", "Store"]

RESERVED = ("__id", "__deleted", "__versions", "__modified_at")


class Store(Protocol):
    """Minimal persistence contract the engine relies on."""

    def load(self) -> Dict[str, Row]: ...

    def save(self, rows: Mapping[str, Row]) -> None: ...


class MemoryStore:
    """In-process store, useful for tests and for dry runs.

    >>> store = MemoryStore({"1": Row("1", {"name": "Ada"})})
    >>> store.save({"2": Row("2", {"name": "Grace"})})
    >>> sorted(store.load())
    ['2']
    """

    def __init__(self, rows: Optional[Mapping[str, Row]] = None, *, name: str = "memory") -> None:
        self._rows: Dict[str, Row] = dict(rows or {})
        self.name = name
        self.saves = 0

    def load(self) -> Dict[str, Row]:
        return dict(self._rows)

    def save(self, rows: Mapping[str, Row]) -> None:
        self._rows = {row_id: row for row_id, row in rows.items()}
        self.saves += 1

    def __len__(self) -> int:
        return len(self._rows)


class JsonFileStore:
    """One JSON document: ``{"version": 1, "rows": [...]}``.

    Writes go to a temporary file in the same directory and are moved into place,
    so a half-written file is never a state a reader can observe.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Row]:
        if not self.path.exists():
            return {}
        text = self.path.read_text(encoding="utf-8-sig")
        if not text.strip():
            return {}
        payload = json.loads(text)
        if isinstance(payload, Mapping):
            raw = payload.get("rows", [])
            if not isinstance(raw, list):
                raise ValueError(f"{self.path}: 'rows' must be a list")
        elif isinstance(payload, list):
            raw = payload
        else:
            raise ValueError(f"{self.path}: expected an object or a list of rows")
        return sort_rows(Row.from_dict(item) for item in raw)

    def save(self, rows: Mapping[str, Row]) -> None:
        payload = {
            "version": 1,
            "rows": [row.to_dict() for row in sorted(rows.values(), key=lambda item: item.id)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temp_path, self.path)
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise


def _cell(text: str) -> Any:
    """CSV cells are text. Empty text is a blank cell; nothing else is inferred.

    Inferring types from CSV is how ``0123`` becomes ``123`` and a phone number
    becomes a float. The adapter does not guess.
    """
    return text if text != "" else None


class CsvStore:
    """Spreadsheet-shaped store: one column per field, plus four reserved ones.

    Values come back as strings, so ``"1"`` and ``1`` are different cells here --
    that is a real property of CSV, and pretending otherwise would corrupt
    identifiers with leading zeros.
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Row]:
        if not self.path.exists():
            return {}
        rows: Dict[str, Row] = {}
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = {"__id"} - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{self.path}: CSV is missing the __id column")
            for record in reader:
                row_id = (record.get("__id") or "").strip()
                if not row_id:
                    continue
                versions: Dict[str, int] = {}
                raw_versions = (record.get("__versions") or "").strip()
                if raw_versions:
                    parsed = json.loads(raw_versions)
                    if not isinstance(parsed, Mapping):
                        raise ValueError(f"{self.path}: __versions must be a JSON object")
                    versions = {str(key): int(value) for key, value in parsed.items()}
                fields = {
                    key: _cell(value)
                    for key, value in record.items()
                    if key not in RESERVED and value is not None
                }
                deleted = str(record.get("__deleted") or "").strip().lower() in {"1", "true", "yes"}
                rows[row_id] = Row(
                    id=row_id,
                    fields=fields,
                    versions=versions,
                    deleted=deleted,
                    modified_at=(record.get("__modified_at") or None),
                )
        return rows

    def save(self, rows: Mapping[str, Row]) -> None:
        ordered = sorted(rows.values(), key=lambda item: item.id)
        columns = sorted({name for row in ordered for name in row.fields})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(list(RESERVED) + columns)
                for row in ordered:
                    cells = [
                        row.id,
                        "1" if row.deleted else "",
                        json.dumps(dict(sorted(row.versions.items())), separators=(",", ":")),
                        row.modified_at or "",
                    ]
                    cells.extend("" if row.get(name) is None else str(row.get(name)) for name in columns)
                    writer.writerow(cells)
            os.replace(temp_path, self.path)
        except BaseException:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

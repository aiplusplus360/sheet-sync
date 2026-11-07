# sheet-sync

**Two-way sync with a three-way merge. Last-write-wins is not a conflict strategy, it is a data-loss strategy with a timestamp.**

Postgres on one side, a shared spreadsheet on the other, and both edited while you were not looking. Zero dependencies, standard library only, Python 3.9+.

```console
$ sheetsync sync --base base.json --left postgres.json --right sheets.json --dry-run
policy: manual   added 1  modified 2  removed 1  held 1

1 row(s) held back:
  ! c-002 price: base=49 left=59 right=69

  ~ c-001 modified  (plan, seats)
  ~ c-003 modified  (price)
  + c-004 added  (name, plan, price)
  - c-005 removed
$ echo $?
3
```

Four rows merged, one row refused: two systems set the same price to different values, and the tool says so instead of picking one.

---

## The failure this fixes

A customer row looks like this in both systems:

| id | name | plan | seats | price |
|---|---|---|---|---|
| c-001 | Acme | pro | 5 | 99 |

Sales changes `plan` to `enterprise` in the sheet. Support adds three seats in the CRM. Both writes are valid, both are non-overlapping, and a last-write-wins sync drops one of them depending on which job ran last. Nobody notices until the invoice is wrong.

Three-way merge fixes that: with the base snapshot both sides last agreed on, each field is decided independently.

| base | left | right | result | why |
|---|---|---|---|---|
| `pro` | `pro` | `enterprise` | `enterprise` | only the right side moved |
| `5` | `8` | `5` | `8` | only the left side moved |
| `49` | `59` | `69` | **conflict** | both moved, differently |

The first two are safe merges. The third is not a merge at all, and the engine does not pretend otherwise.

```console
$ python -m sheetsync sync --base base.json --left postgres.json --right sheets.json --dry-run
  ~ c-001 modified  (plan, seats)     # both edits kept
  ! c-002 price: base=49 left=59 right=69
```

## What a conflict actually is

A conflict is a cell where **both sides changed the value and they disagree**. Everything else has an answer:

- one side changed it → take that side;
- both changed it to the same value → take it, no conflict;
- neither changed it → keep the base;
- one side added a field → keep it;
- one side cleared a field → clear it.

That leaves exactly the case no algorithm can resolve for you. So `manual` is the default: the row is held back, written nowhere, reported with all three values, and the process exits `3`. If you *want* a rule, you choose one and the discarded value is still recorded:

```console
$ sheetsync sync --base base.json --left postgres.json --right sheets.json --policy prefer-newest
  c-002.price: took 69 via policy:prefer-newest
    conflict was postgres=59 sheets=69
written: 1 row(s)
```

`prefer-newest` uses each row's `modified_at`. If either side has no timestamp, or the timestamps are equal, it refuses to guess and the conflict stays open. A tie-break invented at that point is indistinguishable from data loss.

## Deletes

Deletes need the same treatment, and this is where most sync tools quietly corrupt data.

- A deleted row is a **tombstone** (`deleted: true`), not a missing row. A row that is absent from a store has simply never been created there.
- Delete on one side, untouched on the other → the delete propagates.
- Delete on one side, **edit on the other** → `delete_edit` conflict. The edit is not older than the delete and the delete is not newer than the edit; they are different intentions.

```
! c-005 row deleted: base=False left=True right=False
```

`prefer-left` keeps the deletion. `prefer-right` keeps the edited row and records that a delete was overridden. `manual` holds the row.

## Values and comparison

`==` is the wrong equality for a spreadsheet, so every cell is compared through a canonical form:

| left | right | same? | why |
|---|---|---|---|
| `1` | `1.0` | yes | same number, different notation |
| `"1"` | `1` | no | a spreadsheet distinguishes text from number |
| `true` | `1` | no | `True == 1` in Python; that is a bug, not a feature |
| blank | `""` | no | empty cell is not an empty string |
| `NaN` | `NaN` | error | rejected on write: `nan != nan` would report every row changed |

That last row is deliberate. Non-finite numbers raise `ValueError` instead of producing a sync that never converges.

## Versions

Each row carries a per-field counter, incremented by whoever made the change. The merge rule is one line: **take the maximum, and add one only when a policy decided something neither side recorded.** Carrying an existing edit forward does not inflate counters, so the versions mean "how many times this cell changed", not "how many times this job ran".

```console
$ sheetsync diff --base base.json --side postgres.json
5 changed row(s) in postgres.json
  ~ c-001 modified
      seats: 5 -> 8
  ~ c-002 modified
      price: 49 -> 59
  + c-004 added
      name: None -> 'Umbrella'
  - c-005 removed
```

## CLI

```console
$ sheetsync sync --base base.json --left postgres.json --right sheets.json \
    --journal sync-log.jsonl --update-base
policy: manual   added 1  modified 2  removed 1  held 1
...
written: 4 row(s)
base snapshot updated
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | plan and report, write nothing |
| `--policy` | `manual` (default), `prefer-left`, `prefer-right`, `prefer-newest` |
| `--journal` | append every decision to a JSONL audit log |
| `--update-base` | re-baseline the base file so the next run is a no-op |
| `--now` | fixed timestamp, for reproducible runs |
| `--json` | machine-readable plan/report |

| Exit code | Meaning |
| --- | --- |
| `0` | nothing to write, no conflicts |
| `1` | rows were written, no conflicts left |
| `2` | error (missing file, malformed JSON, unknown policy) |
| `3` | at least one row held back; outranks `1`, since a conflict needs a human |

`3` takes precedence over `1` on purpose: a run that wrote four rows and dropped one into a conflict should page someone, not look like a success. It also means a *settled* run with an open conflict reports `3` forever -- "nothing to write" and "resolved" are different states, and only one of them is fine.

Rows are independent: an unresolved conflict does not block the other rows, and it does not get partially applied either. A row with one conflicting field and one clean field is held entirely -- half a merged row is worse than none.

`sheetsync diff --base ... --side ...` answers "what did this side change", and `sheetsync log --journal ... --row c-002` prints one row's history.

## Wiring a real store

The interface is two methods, because a sync engine that needs a session object to read a row is a sync engine nobody runs:

```python
from sheetsync import JsonFileStore, Row, sync

class PostgresStore:
    def __init__(self, connection, table="customers"):
        self.connection, self.table = connection, table

    def load(self):
        with self.connection.cursor() as cursor:
            cursor.execute(f"select id, fields, versions, deleted, modified_at from {self.table}")
            return {
                row_id: Row(row_id, fields, versions, deleted, modified_at)
                for row_id, fields, versions, deleted, modified_at in cursor.fetchall()
            }

    def save(self, rows):
        with self.connection.cursor() as cursor:
            for row in rows.values():
                cursor.execute(
                    f"insert into {self.table} (id, fields, versions, deleted, modified_at) "
                    "values (%s, %s, %s, %s, %s) on conflict (id) do update set "
                    "fields = excluded.fields, versions = excluded.versions, "
                    "deleted = excluded.deleted, modified_at = excluded.modified_at",
                    (row.id, dict(row.fields), dict(row.versions), row.deleted, row.modified_at),
                )
        self.connection.commit()

sync(JsonFileStore("base.json"), PostgresStore(conn), GoogleSheetStore(sheet), update_base=JsonFileStore("base.json"))
```

Three shipped adapters cover the cases that need no credentials: `MemoryStore` (tests, dry runs), `JsonFileStore` (atomic writes via temp file + `os.replace`), `CsvStore` (spreadsheet-shaped, values stay text so `0123` still has its leading zero).

## The journal

Every decision is appended as one JSON object per line, so a crash mid-sync leaves the record intact:

```json
{"applied": false, "conflicts": [{"base": 49, "field": "price", "kind": "value", "left": 59, "resolved": false, "right": 69, "row": "c-002"}], "fields": [], "kind": "conflict", "row": "c-002", "ts": "2026-11-07T12:00:00+00:00"}
{"applied": true, "conflicts": [], "fields": [{"after": 69, "before": 49, "field": "price", "row": "c-002"}], "kind": "modified", "row": "c-002", "ts": "2026-11-07T12:05:00+00:00"}
```

The second line is the resolution: what was taken, and the first line still says what both sides wanted. That is the difference between an audit trail and a changelog.

---

## Architecture

```
sheetsync/
  model.py      Row, canonical cell comparison, row_diff
  merge.py      three-way merge per field, conflicts, policies
  engine.py     plan_sync (pure) and apply_plan (the only writer)
  changelog.py  append-only JSONL journal
  adapters.py   MemoryStore, JsonFileStore, CsvStore
  cli.py        sync | diff | log
```

`plan_sync` never touches a store: three snapshots in, a decision list out. That is what makes `--dry-run` trustworthy rather than a second code path that mostly agrees with the real one. `apply_plan` writes only rows the plan marked applicable, once per store, after loading it once.

## What it does not do

- **No transaction across stores.** Each store is saved once, but two stores cannot be committed atomically. If the second save fails after the first, the journal records the plan and the next run converges; the report is the record of what happened, not a rollback.
- **No schema migration.** `save` writes the whole row set, which is fine for a spreadsheet and wrong for a billion-row table. Add a `save_delta` for that; the merge itself is unchanged.
- **No field-level history beyond the journal.** Versions count changes, they do not store old values.
- **No UI, no auth, no scheduler.** It is the merge core and a CLI, not a product.
- **CSV is lossy by design.** Everything is text, and the adapter refuses to infer types -- inferring is how `0123` becomes `123`.

## Tests

```bash
python -m unittest discover -s tests -v
```

92 tests and 31 doctests: canonical comparison (`1` vs `1.0` vs `True` vs `"1"`), field-level merge for every one-sided/both-sided/agreed case, unresolved conflicts not written, whole-row blocking, delete propagation, delete-versus-edit under all three policies, resurrection, version arithmetic, idempotency after re-baselining, stale-base re-merge, journal append and history, JSON atomic writes with no temp file left behind, BOM tolerance, CSV type fidelity and leading zeros, and CLI exit codes `0/1/2/3`.

## License

MIT (c) 2026 aiplusplus360

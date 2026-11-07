"""Tests for sheet-sync."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from sheetsync import (
    CsvStore,
    Journal,
    JsonFileStore,
    MemoryStore,
    Row,
    apply_plan,
    canonical,
    diff_fields,
    diff_side,
    merge_many,
    merge_rows,
    plan_sync,
    row_diff,
    same,
    sync,
)
from sheetsync.cli import main
from sheetsync.merge import PRESENCE_FIELD


def row(row_id: str, **fields) -> Row:
    return Row(row_id, fields)


class CanonicalTests(unittest.TestCase):
    def test_int_and_float_are_the_same_cell(self):
        self.assertTrue(same(1, 1.0))
        self.assertTrue(same(1299.0, 1299))

    def test_booleans_are_not_integers(self):
        self.assertFalse(same(True, 1))
        self.assertFalse(same(False, 0))

    def test_numbers_are_not_text(self):
        self.assertFalse(same("1", 1))
        self.assertFalse(same("", None))

    def test_blank_is_only_none(self):
        self.assertTrue(same(None, None))
        self.assertFalse(same(None, 0))

    def test_nested_values(self):
        self.assertTrue(same({"a": [1, 2.0]}, {"a": [1.0, 2]}))
        self.assertFalse(same({"a": 1}, {"a": 2}))

    def test_non_finite_numbers_are_rejected(self):
        with self.assertRaises(ValueError):
            canonical(float("nan"))
        with self.assertRaises(ValueError):
            canonical(float("inf"))


class RowTests(unittest.TestCase):
    def test_blank_fields_are_dropped(self):
        self.assertEqual(row("1", a=1, b=None).fields, {"a": 1})

    def test_id_is_required(self):
        with self.assertRaises(ValueError):
            Row("")

    def test_versions_default_to_zero(self):
        self.assertEqual(row("1", a=1).version("a"), 0)
        self.assertEqual(row("1", a=1).version("missing"), 0)

    def test_bump_is_a_copy(self):
        original = row("1", a=1)
        bumped = original.bump(["a"])
        self.assertEqual(original.version("a"), 0)
        self.assertEqual(bumped.version("a"), 1)

    def test_tombstone_keeps_identity(self):
        dead = row("1", a=1).bump(["a"]).tombstone()
        self.assertTrue(dead.deleted)
        self.assertEqual(dead.fields, {})
        self.assertEqual(dead.version("a"), 1)

    def test_round_trip(self):
        original = Row("1", {"a": 1, "b": "x"}, {"a": 2}, deleted=True, modified_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(Row.from_dict(original.to_dict()), original)

    def test_from_dict_rejects_malformed_rows(self):
        with self.assertRaises(ValueError):
            Row.from_dict({"fields": {}})
        with self.assertRaises(ValueError):
            Row.from_dict({"id": "1", "fields": "nope"})

    def test_hash_and_equality(self):
        self.assertEqual(row("1", a=1), Row("1", {"a": 1.0}))
        self.assertNotEqual(row("1", a=1), row("1", a=2))


class DiffTests(unittest.TestCase):
    def test_unchanged(self):
        base = row("1", a=1, b="x")
        self.assertEqual(row_diff("1", base, row("1", a=1, b="x")).kind, "unchanged")

    def test_modified_lists_every_changed_field(self):
        diff = row_diff("1", row("1", a=1, b="x"), row("1", a=2, b="x"))
        self.assertEqual(diff.kind, "modified")
        self.assertEqual([change.field for change in diff.changes], ["a"])

    def test_field_order_is_stable(self):
        changes = diff_fields("1", row("1"), row("1", z=1, a=2, m=3))
        self.assertEqual([change.field for change in changes], ["a", "m", "z"])

    def test_added_and_removed(self):
        self.assertEqual(row_diff("1", None, row("1", a=1)).kind, "added")
        self.assertEqual(row_diff("1", row("1", a=1), None).kind, "removed")

    def test_tombstone_is_removed_and_revived_is_resurrected(self):
        base = row("1", a=1)
        self.assertEqual(row_diff("1", base, base.tombstone()).kind, "removed")
        self.assertEqual(row_diff("1", base.tombstone(), base).kind, "resurrected")

    def test_clearing_a_field_is_a_change(self):
        changes = diff_fields("1", row("1", a=1, b=2), row("1", b=2))
        self.assertEqual([(c.field, c.before, c.after) for c in changes], [("a", 1, None)])


class MergeFieldTests(unittest.TestCase):
    def test_one_sided_edit_wins(self):
        result = merge_rows("1", row("1", price=10), row("1", price=12), row("1", price=10))
        self.assertEqual(result.kind, "modified")
        self.assertEqual(result.merged.get("price"), 12)
        self.assertEqual(result.conflicts, ())

    def test_both_sides_agree(self):
        result = merge_rows("1", row("1", price=10), row("1", price=12), row("1", price=12))
        self.assertEqual(result.kind, "modified")
        self.assertEqual(result.merged.get("price"), 12)
        self.assertEqual(result.conflicts, ())

    def test_both_sides_disagree_is_a_conflict(self):
        result = merge_rows("1", row("1", price=10), row("1", price=12), row("1", price=15))
        self.assertEqual(result.kind, "conflict")
        self.assertFalse(result.applicable)
        self.assertEqual(len(result.conflicts), 1)
        conflict = result.conflicts[0]
        self.assertEqual((conflict.base, conflict.left, conflict.right), (10, 12, 15))
        self.assertTrue(conflict.unresolved)

    def test_conflict_report_keeps_base_value_in_memory(self):
        result = merge_rows("1", row("1", price=10), row("1", price=12), row("1", price=15))
        # Not applicable, so this value is never written; it is only there so the
        # row stays coherent for reporting.
        self.assertEqual(result.merged.get("price"), 10)

    def test_prefer_left_records_what_was_dropped(self):
        result = merge_rows(
            "1", row("1", price=10), row("1", price=12), row("1", price=15), policy="prefer-left"
        )
        self.assertTrue(result.applicable)
        self.assertEqual(result.merged.get("price"), 12)
        conflict = result.conflicts[0]
        self.assertEqual(conflict.resolved_by, "policy:prefer-left")
        self.assertEqual(conflict.right, 15)

    def test_prefer_right(self):
        result = merge_rows(
            "1", row("1", price=10), row("1", price=12), row("1", price=15), policy="prefer-right"
        )
        self.assertEqual(result.merged.get("price"), 15)
        self.assertEqual(result.conflicts[0].resolution, 15)

    def test_prefer_newest_uses_timestamps(self):
        left = Row("1", {"price": 12}, modified_at="2026-01-02T00:00:00+00:00")
        right = Row("1", {"price": 15}, modified_at="2026-01-03T00:00:00+00:00")
        result = merge_rows("1", row("1", price=10), left, right, policy="prefer-newest")
        self.assertEqual(result.merged.get("price"), 15)
        self.assertEqual(result.conflicts[0].resolved_by, "policy:prefer-newest")

    def test_prefer_newest_refuses_to_guess(self):
        result = merge_rows(
            "1", row("1", price=10), row("1", price=12), row("1", price=15), policy="prefer-newest"
        )
        self.assertTrue(result.unresolved)

    def test_unknown_policy_is_an_error(self):
        with self.assertRaises(ValueError):
            merge_rows("1", None, row("1", a=1), row("1", a=2), policy="coin-flip")

    def test_independent_fields_merge_cleanly(self):
        base = row("1", price=10, note="old")
        left = row("1", price=12, note="old")
        right = row("1", price=10, note="new")
        result = merge_rows("1", base, left, right)
        self.assertEqual(result.merged.fields, {"price": 12, "note": "new"})
        self.assertEqual(result.conflicts, ())

    def test_field_added_on_one_side(self):
        result = merge_rows("1", row("1", a=1), row("1", a=1, b=2), row("1", a=1))
        self.assertEqual(result.merged.fields, {"a": 1, "b": 2})

    def test_field_cleared_on_one_side(self):
        result = merge_rows("1", row("1", a=1, b=2), row("1", b=2), row("1", a=1, b=2))
        self.assertNotIn("a", result.merged.fields)
        self.assertEqual([change.field for change in result.changes], ["a"])


class MergePresenceTests(unittest.TestCase):
    def test_added_on_one_side_is_copied(self):
        result = merge_rows("9", None, row("9", a=1), None)
        self.assertEqual(result.kind, "added")
        self.assertEqual(result.merged.fields, {"a": 1})

    def test_added_on_both_sides_same_values(self):
        result = merge_rows("9", None, row("9", a=1), row("9", a=1))
        self.assertEqual(result.kind, "added")
        self.assertEqual(result.conflicts, ())

    def test_added_on_both_sides_different_values(self):
        result = merge_rows("9", None, row("9", a=1), row("9", a=2))
        self.assertEqual(result.kind, "conflict")
        self.assertEqual(result.conflicts[0].field, "a")

    def test_delete_propagates(self):
        base = row("1", a=1)
        result = merge_rows("1", base, base.tombstone(), base)
        self.assertEqual(result.kind, "removed")
        self.assertTrue(result.merged.deleted)
        self.assertEqual(result.conflicts, ())

    def test_delete_versus_edit_is_a_conflict(self):
        base = row("1", a=1)
        result = merge_rows("1", base, base.tombstone(), row("1", a=2))
        self.assertEqual(result.kind, "conflict")
        conflict = result.conflicts[0]
        self.assertEqual(conflict.kind, "delete_edit")
        self.assertEqual(conflict.field, PRESENCE_FIELD)

    def test_delete_versus_edit_prefer_delete(self):
        base = row("1", a=1)
        result = merge_rows("1", base, base.tombstone(), row("1", a=2), policy="prefer-left")
        self.assertTrue(result.merged.deleted)
        self.assertEqual(result.kind, "removed")

    def test_delete_versus_edit_prefer_edit(self):
        base = row("1", a=1)
        result = merge_rows("1", base, base.tombstone(), row("1", a=2), policy="prefer-right")
        self.assertFalse(result.merged.deleted)
        self.assertEqual(result.merged.get("a"), 2)
        # The row was never deleted in the merged result, so it is a modification
        # relative to base; the rejected delete is still recorded as a conflict.
        self.assertEqual(result.kind, "modified")
        self.assertEqual(result.conflicts[0].resolution, False)

    def test_resurrection_from_one_side(self):
        base = row("1", a=1).tombstone()
        result = merge_rows("1", base, row("1", a=1), base)
        self.assertFalse(result.merged.deleted)
        self.assertEqual(result.kind, "resurrected")

    def test_both_sides_absent_is_not_a_deletion(self):
        result = merge_rows("1", row("1", a=1), None, None)
        self.assertEqual(result.kind, "unchanged")
        self.assertIsNone(result.merged)

    def test_nothing_at_all(self):
        result = merge_rows("1", None, None, None)
        self.assertEqual(result.kind, "unchanged")
        self.assertIsNone(result.merged)


class VersionTests(unittest.TestCase):
    def test_carrying_a_change_does_not_inflate_versions(self):
        left = Row("1", {"a": 2}, {"a": 3})
        right = Row("1", {"a": 1}, {"a": 2})
        result = merge_rows("1", row("1", a=1), left, right)
        self.assertEqual(result.merged.version("a"), 3)

    def test_policy_resolution_bumps_the_version(self):
        left = Row("1", {"a": 2}, {"a": 3})
        right = Row("1", {"a": 5}, {"a": 3})
        result = merge_rows("1", row("1", a=1), left, right, policy="prefer-left")
        self.assertEqual(result.merged.version("a"), 4)
        self.assertEqual(result.merged.get("a"), 2)

    def test_unresolved_conflict_does_not_bump(self):
        left = Row("1", {"a": 2}, {"a": 3})
        right = Row("1", {"a": 5}, {"a": 3})
        result = merge_rows("1", row("1", a=1), left, right)
        self.assertEqual(result.merged.version("a"), 3)


class MergeManyTests(unittest.TestCase):
    def test_rows_are_ordered_and_unioned(self):
        base = {"b": row("b", a=1)}
        left = {"c": row("c", a=1), "b": row("b", a=2)}
        right = {"a": row("a", a=1), "b": row("b", a=1)}
        results = merge_many(base, left, right)
        self.assertEqual([result.row_id for result in results], ["a", "b", "c"])
        kinds = {result.row_id: result.kind for result in results}
        self.assertEqual(kinds["a"], "added")
        self.assertEqual(kinds["b"], "modified")
        self.assertEqual(kinds["c"], "added")


class PlanTests(unittest.TestCase):
    def test_counts(self):
        base = {"1": row("1", price=10), "2": row("2", a=1)}
        left = {"1": row("1", price=12), "2": row("2", a=1), "3": row("3", x="y")}
        right = {"1": row("1", price=10), "2": row("2", a=1).tombstone()}
        plan = plan_sync(base, left, right)
        counts = plan.counts
        self.assertEqual(counts["modified"], 1)
        self.assertEqual(counts["added"], 1)
        self.assertEqual(counts["removed"], 1)

    def test_conflict_blocks_the_whole_row(self):
        base = {"1": row("1", price=10, note="a")}
        left = {"1": row("1", price=12, note="changed")}
        right = {"1": row("1", price=15, note="a")}
        plan = plan_sync(base, left, right)
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.changes, ())
        result = plan.conflicts[0]
        # The row is held back entirely, but the report still shows the edit it
        # would have applied -- "not applied" and "nothing detected" differ.
        self.assertEqual([change.field for change in result.changes], ["note"])
        self.assertFalse(result.applicable)

    def test_resolved_conflicts_are_applicable(self):
        base = {"1": row("1", price=10)}
        left = {"1": row("1", price=12)}
        right = {"1": row("1", price=15)}
        plan = plan_sync(base, left, right, policy="prefer-left")
        self.assertEqual(plan.conflicts, ())
        self.assertEqual(len(plan.resolved), 1)
        self.assertEqual(plan.applicable[0].merged.get("price"), 12)

    def test_empty_plan(self):
        plan = plan_sync({}, {}, {})
        self.assertEqual(plan.results, ())
        self.assertEqual(plan.counts["added"], 0)

    def test_plan_serialises(self):
        plan = plan_sync({"1": row("1", a=1)}, {"1": row("1", a=2)}, {"1": row("1", a=1)})
        payload = plan.to_dict()
        self.assertEqual(payload["policy"], "manual")
        self.assertEqual(payload["rows"][0]["kind"], "modified")
        self.assertTrue(payload["rows"][0]["applied"])

    def test_diff_side(self):
        changes = diff_side({"1": row("1", a=1), "2": row("2", b=1)}, {"1": row("1", a=2), "2": row("2", b=1)})
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["row"], "1")
        self.assertEqual(changes[0]["fields"][0]["after"], 2)


class ApplyTests(unittest.TestCase):
    def test_applies_to_both_sides(self):
        base = MemoryStore({"1": row("1", price=10)})
        left = MemoryStore({"1": row("1", price=12)})
        right = MemoryStore({"1": row("1", price=10)})
        report = sync(base, left, right)
        self.assertEqual(report.written, ("1",))
        self.assertEqual(left.load()["1"].get("price"), 12)
        self.assertEqual(right.load()["1"].get("price"), 12)

    def test_conflicts_are_not_written(self):
        base = MemoryStore({"1": row("1", price=10)})
        left = MemoryStore({"1": row("1", price=12)})
        right = MemoryStore({"1": row("1", price=15)})
        report = sync(base, left, right)
        self.assertEqual(report.written, ())
        self.assertEqual(left.load()["1"].get("price"), 12)
        self.assertEqual(right.load()["1"].get("price"), 15)

    def test_unrelated_rows_still_apply_when_one_row_conflicts(self):
        base = MemoryStore({"1": row("1", price=10), "2": row("2", a=1)})
        left = MemoryStore({"1": row("1", price=12), "2": row("2", a=2)})
        right = MemoryStore({"1": row("1", price=15), "2": row("2", a=1)})
        report = sync(base, left, right)
        self.assertEqual(report.written, ("2",))
        self.assertEqual(right.load()["2"].get("a"), 2)

    def test_new_row_propagates(self):
        base = MemoryStore()
        left = MemoryStore({"9": row("9", name="new")})
        right = MemoryStore()
        sync(base, left, right)
        self.assertEqual(right.load()["9"].fields, {"name": "new"})

    def test_delete_propagates(self):
        base = MemoryStore({"1": row("1", a=1)})
        left = MemoryStore({"1": row("1", a=1).tombstone()})
        right = MemoryStore({"1": row("1", a=1)})
        sync(base, left, right)
        self.assertTrue(right.load()["1"].deleted)
        self.assertTrue(left.load()["1"].deleted)

    def test_second_run_with_updated_base_is_a_no_op(self):
        base = MemoryStore({"1": row("1", price=10)})
        left = MemoryStore({"1": row("1", price=12)})
        right = MemoryStore({"1": row("1", price=10)})
        first = sync(base, left, right, update_base=base)
        second = sync(base, left, right, update_base=base)
        self.assertEqual(first.written, ("1",))
        self.assertEqual(second.written, ())
        self.assertEqual(second.plan.counts["modified"], 0)

    def test_stale_base_rewrites_the_same_row(self):
        # Documented consequence of not re-baselining: the merge is re-derived
        # even though the value already matches. --update-base avoids this.
        base = MemoryStore({"1": row("1", price=10)})
        left = MemoryStore({"1": row("1", price=12)})
        right = MemoryStore({"1": row("1", price=10)})
        sync(base, left, right)
        second = sync(base, left, right)
        self.assertEqual(second.written, ("1",))

    def test_apply_without_stores_still_reports_the_plan(self):
        plan = plan_sync({"1": row("1", a=1)}, {"1": row("1", a=2)}, {"1": row("1", a=1)})
        report = apply_plan(plan, [])
        self.assertEqual(report.written, ("1",))

    def test_write_failures_surface(self):
        class Boom(MemoryStore):
            def save(self, rows):
                raise OSError("disk is on fire")

        plan = plan_sync({"1": row("1", a=1)}, {"1": row("1", a=2)}, {"1": row("1", a=1)})
        with self.assertRaises(OSError):
            apply_plan(plan, [Boom()])


class JournalTests(unittest.TestCase):
    def test_journal_records_applied_and_held_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            journal = Journal(path)
            base = MemoryStore({"1": row("1", price=10), "2": row("2", a=1)})
            left = MemoryStore({"1": row("1", price=12), "2": row("2", a=2)})
            right = MemoryStore({"1": row("1", price=15), "2": row("2", a=1)})
            sync(base, left, right, journal=journal, now="2026-01-01T00:00:00+00:00")
            entries = journal.entries()
            self.assertEqual([entry["row"] for entry in entries], ["1", "2"])
            self.assertEqual(entries[0]["kind"], "conflict")
            self.assertFalse(entries[0]["applied"])
            self.assertEqual(entries[1]["applied"], True)

    def test_journal_is_append_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            journal = Journal(path)
            journal.record({"row": "1"}, now="a")
            journal.record({"row": "2"}, now="b")
            self.assertEqual([entry["row"] for entry in journal.entries()], ["1", "2"])

    def test_history_filters_by_row(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "log.jsonl")
            journal.record({"row": "1", "kind": "modified"}, now="a")
            journal.record({"row": "2", "kind": "added"}, now="b")
            journal.record({"row": "1", "kind": "removed"}, now="c")
            self.assertEqual([entry["kind"] for entry in journal.history("1")], ["modified", "removed"])
            self.assertEqual(journal.rows_touched(), ["1", "2"])

    def test_malformed_line_names_the_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            path.write_text('{"row": "1"}\nnot json\n', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                Journal(path).entries()
            self.assertIn(":2", str(caught.exception))

    def test_missing_journal_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(Journal(Path(folder) / "nope.jsonl").entries(), [])


class JsonStoreTests(unittest.TestCase):
    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(JsonFileStore(Path(folder) / "x.json").load(), {})

    def test_round_trip_preserves_types(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.json"
            store = JsonFileStore(path)
            original = {
                "1": Row("1", {"price": 12, "name": "Widget", "live": True}, {"price": 2}),
                "2": Row("2", {}, deleted=True),
            }
            store.save(original)
            loaded = store.load()
            self.assertEqual(loaded["1"].fields, {"price": 12, "name": "Widget", "live": True})
            self.assertEqual(loaded["1"].version("price"), 2)
            self.assertTrue(loaded["2"].deleted)

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.json"
            JsonFileStore(path).save({"1": row("1", a=1)})
            self.assertEqual(sorted(os.listdir(folder)), ["rows.json"])

    def test_accepts_a_bare_list(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.json"
            path.write_text('[{"id": "1", "fields": {"a": 1}}]', encoding="utf-8")
            self.assertEqual(JsonFileStore(path).load()["1"].fields, {"a": 1})

    def test_empty_file_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.json"
            path.write_text("", encoding="utf-8")
            self.assertEqual(JsonFileStore(path).load(), {})

    def test_malformed_json_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.json"
            path.write_text("{nope", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                JsonFileStore(path).load()

    def test_bom_is_tolerated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.json"
            path.write_text('\ufeff{"rows": [{"id": "1", "fields": {"a": 1}}]}', encoding="utf-8")
            self.assertEqual(JsonFileStore(path).load()["1"].fields, {"a": 1})


class CsvStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.csv"
            store = CsvStore(path)
            store.save({"1": Row("1", {"name": "Ada", "code": "0123"}, {"name": 1}, modified_at="t")})
            loaded = store.load()
            self.assertEqual(loaded["1"].fields, {"name": "Ada", "code": "0123"})
            self.assertEqual(loaded["1"].version("name"), 1)
            self.assertEqual(loaded["1"].modified_at, "t")

    def test_values_stay_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.csv"
            path.write_text("__id,__deleted,__versions,__modified_at,price\n1,,,,10\n", encoding="utf-8")
            self.assertEqual(CsvStore(path).load()["1"].fields["price"], "10")

    def test_blank_cell_is_none(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.csv"
            path.write_text("__id,__deleted,__versions,__modified_at,a,b\n1,,,,,\n", encoding="utf-8")
            self.assertEqual(CsvStore(path).load()["1"].fields, {})

    def test_deleted_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.csv"
            path.write_text("__id,__deleted,__versions,__modified_at,a\n1,1,{},\n", encoding="utf-8")
            self.assertTrue(CsvStore(path).load()["1"].deleted)

    def test_missing_id_column_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.csv"
            path.write_text("name\nAda\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                CsvStore(path).load()

    def test_bad_versions_raise(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.csv"
            path.write_text("__id,__deleted,__versions,__modified_at,a\n1,,[1],\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                CsvStore(path).load()

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(CsvStore(Path(folder) / "x.csv").load(), {})


class CliTests(unittest.TestCase):
    def write(self, folder: str, name: str, rows) -> str:
        path = Path(folder) / name
        payload = {"version": 1, "rows": [row.to_dict() for row in rows]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def run_cli(self, *argv) -> tuple:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
        return code, out.getvalue()

    def test_dry_run_reports_conflict_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=12)])
            right = self.write(folder, "right.json", [row("1", price=15)])
            code, out = self.run_cli("sync", "--base", base, "--left", left, "--right", right, "--dry-run")
            self.assertEqual(code, 3)
            self.assertIn("held back", out)
            self.assertEqual(JsonFileStore(right).load()["1"].get("price"), 15)

    def test_dry_run_clean_is_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=10)])
            right = self.write(folder, "right.json", [row("1", price=10)])
            code, out = self.run_cli("sync", "--base", base, "--left", left, "--right", right, "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("nothing to do", out)

    def test_apply_exits_one_and_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=12)])
            right = self.write(folder, "right.json", [row("1", price=10)])
            code, out = self.run_cli("sync", "--base", base, "--left", left, "--right", right)
            self.assertEqual(code, 1)
            self.assertIn("written: 1 row(s)", out)
            self.assertEqual(JsonFileStore(right).load()["1"].get("price"), 12)

    def test_json_output_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=12)])
            right = self.write(folder, "right.json", [row("1", price=10)])
            code, out = self.run_cli("sync", "--base", base, "--left", left, "--right", right, "--json")
            payload = json.loads(out)
            self.assertEqual(code, 1)
            self.assertEqual(payload["policy"], "manual")
            self.assertEqual(payload["written"], ["1"])
            self.assertEqual(payload["rows"][0]["kind"], "modified")

    def test_update_base_makes_the_next_run_a_no_op(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=12)])
            right = self.write(folder, "right.json", [row("1", price=10)])
            first, _ = self.run_cli("sync", "--base", base, "--left", left, "--right", right, "--update-base")
            second, _ = self.run_cli("sync", "--base", base, "--left", left, "--right", right, "--update-base")
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)

    def test_policy_resolves_and_reports_written(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=12)])
            right = self.write(folder, "right.json", [row("1", price=15)])
            code, out = self.run_cli(
                "sync", "--base", base, "--left", left, "--right", right, "--policy", "prefer-right"
            )
            self.assertEqual(code, 1)
            self.assertIn("policy:prefer-right", out)
            self.assertEqual(JsonFileStore(left).load()["1"].get("price"), 15)

    def test_journal_is_written(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left = self.write(folder, "left.json", [row("1", price=12)])
            right = self.write(folder, "right.json", [row("1", price=10)])
            log = str(Path(folder) / "log.jsonl")
            self.run_cli(
                "sync", "--base", base, "--left", left, "--right", right, "--journal", log,
                "--now", "2026-01-01T00:00:00+00:00",
            )
            entries = Journal(log).entries()
            self.assertEqual(entries[0]["ts"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(entries[0]["fields"][0]["after"], 12)

    def test_diff_command(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", a=1)])
            side = self.write(folder, "side.json", [row("1", a=2), row("2", b=1)])
            code, out = self.run_cli("diff", "--base", base, "--side", side)
            self.assertEqual(code, 0)
            self.assertIn("2 changed row(s)", out)
            self.assertIn("a: 1 -> 2", out)

    def test_log_command(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(Path(folder) / "log.jsonl")
            journal.record({"row": "1", "kind": "modified", "applied": True, "fields": [], "conflicts": []}, now="t")
            code, out = self.run_cli("log", "--journal", str(journal.path))
            self.assertEqual(code, 0)
            self.assertIn("t  1  modified", out)

    def test_csv_store_through_the_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            base = self.write(folder, "base.json", [row("1", price=10)])
            left_path = Path(folder) / "left.csv"
            CsvStore(left_path).save({"1": Row("1", {"price": "12"})})
            right = self.write(folder, "right.json", [row("1", price=10)])
            code, out = self.run_cli(
                "sync", "--base", base, "--left", str(left_path), "--right", right, "--json"
            )
            payload = json.loads(out)
            self.assertEqual(code, 1)
            self.assertEqual(payload["rows"][0]["kind"], "modified")
            self.assertEqual(JsonFileStore(right).load()["1"].get("price"), "12")

    def test_missing_file_exits_two(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "nope.json")
            code, _ = self.run_cli("diff", "--side", missing)
            self.assertEqual(code, 2)

    def test_unknown_policy_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["sync", "--left", "a.json", "--right", "b.json", "--policy", "vibes"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

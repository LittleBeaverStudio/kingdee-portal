"""Complete paged lists, exact totals, per-user snapshots and filter scopes."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import bill_lists as b
import list_filters


class ListsTests(unittest.TestCase):
    def setUp(self):
        self.kd = Mock()
        self.rl = SimpleNamespace(KSVC_SUFFIX="svc.")
        bound = patch.object(b.actions, "get_client", return_value=(self.kd, self.rl, ""))
        bound.start(); self.addCleanup(bound.stop)
        self.filters = list_filters.parse({"kind": "expense"})

    def row(self, fid):
        return [fid, "BX-"+str(fid), "2026-11-18", "C", 10.5, "tester", "101", "org"]

    def test_all_rows_beyond_200_and_1000_are_accessible_and_totals_do_not_change_on_page(self):
        rows = [self.row(fid) for fid in range(1235, 0, -1)]
        self.kd._post.side_effect = [rows[:1000], rows[1000:]]
        cache = {}
        first = b.page("", False, self.filters, None, 1, 20, cache)
        last = b.page("", False, self.filters, None, 62, 20, cache)
        self.assertEqual(first["pagination"]["total"], 1235)
        self.assertEqual(first["summary"]["approved"]["amount"], 12967.5)
        self.assertEqual(last["summary"], first["summary"])
        self.assertEqual(len(last["rows"]), 15)
        self.assertEqual(last["rows"][-1]["fid"], 1)
        self.assertFalse(last["pagination"]["has_next"])
        self.assertEqual(self.kd._post.call_count, 2)
        self.assertEqual(json.loads(self.kd._post.call_args.args[1]["data"])["StartRow"], 1000)

    def test_merge_forms_deterministically_even_when_ids_overlap(self):
        self.filters["kind"] = ""
        self.kd._post.side_effect = [[self.row(2), self.row(1)], [self.row(3), self.row(1)]]
        result = b.page("", False, self.filters, None)
        self.assertEqual(result["pagination"]["total"], 4)
        self.assertEqual([r["fid"] for r in result["rows"]], [3, 2, 1, 1])
        self.assertNotEqual(result["rows"][2]["kind"], result["rows"][3]["kind"])

    def test_top_organization_scope_is_upstream_and_empty_scope_never_returns_all(self):
        self.kd._post.return_value = []
        b.page("", False, self.filters, ["102"])
        where = json.loads(self.kd._post.call_args.args[1]["data"])["FilterString"]
        self.assertIn("FOrgId.FNumber in ('102')", where)
        self.kd.reset_mock()
        self.assertEqual(b.page("", False, self.filters, [])["pagination"]["total"], 0)
        self.kd._post.assert_not_called()

    def test_page_clamps_after_deletion_refresh_and_snapshot_never_crosses_session_or_filters(self):
        cache = {}
        self.kd._post.return_value = [self.row(1)]
        a = b.page("", False, self.filters, None, 100, 20, cache)
        self.assertEqual(a["pagination"]["page"], 1)
        self.kd._post.return_value = []
        self.assertEqual(b.page("", False, self.filters, None, cache=cache)["pagination"]["total"], 1)
        self.assertEqual(b.page("", False, self.filters, None, cache={})["pagination"]["total"], 0)
        self.assertEqual(b.page("", False, {**self.filters,"q":"other"}, None, cache=cache)["pagination"]["total"], 0)
        self.assertEqual(b.page("", False, self.filters, None, cache=cache, refresh=True)["pagination"]["total"], 0)

    def test_application_enriches_only_selected_page_and_deduplicates_detail_amounts(self):
        rows = [[i,"REQ-"+str(i),"2026-11-18","C",True,15,"tester","101","org",""] for i in range(45,0,-1)]
        def post(path, payload):
            data = json.loads(payload["data"])
            if data["FieldKeys"] == "FID,FEntity_FEntryID,FPUSHREIMBAMT":
                ids = data["FilterString"].split('(')[1].rstrip(')').split(',')
                self.assertEqual(ids, [str(i) for i in range(25,5,-1)])
                return [[int(fid),1,3] for fid in ids] * 2
            return rows
        self.kd._post.side_effect = post
        result = b.page("", True, self.filters, None, 2, 20)
        self.assertEqual(result["summary"]["count"], 45)
        self.assertEqual(result["summary"]["amount"], 675)
        self.assertEqual([r["pushed_reimb_amount"] for r in result["rows"]], [3]*20)

    def test_invalid_page_size_is_rejected(self):
        for number, size in ((0,20),(1,999),(-1,20)):
            with self.assertRaises(ValueError): b.page("",False,self.filters,None,number,size)
        self.kd._post.assert_not_called()


if __name__ == '__main__': unittest.main()

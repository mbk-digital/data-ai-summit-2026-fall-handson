"""Re-preparing a Project after the cleanup deleted its agents (docs/maintainers/drafts/deleted-agent-reuse.md).

The fake service mirrors what get returned for a deleted agent on 2026-10-31
(docs/maintainers/validation/deleted-agent-probe.md): HTTP 200 with deleteTime and purgeTime.
"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from workshop.cloud import CloudError
from workshop.data_agent import AgentClient, candidate_ids, context, deletion_state, fingerprint


class LostResponse(CloudError):
    """The create request was sent but its response never arrived."""


class FakeService:
    def __init__(self, client):
        self.client, self.agents, self.calls, self.clock = client, {}, [], 0
        self.fail_create = None  # None, "before" (not sent) or "after" (created, response lost)

    def tick(self):
        self.clock += 1
        return "2026-11-01T00:00:%02dZ" % self.clock

    def request(self, method, url, body=None, missing_ok=False):
        self.calls.append((method, url))
        path = url[len(self.client.base):]
        if method == "GET":
            return copy.deepcopy(self.agents.get(path))
        if method == "POST":
            ident = path.split("dataAgentId=", 1)[1]
            name = self.client.parent + "/dataAgents/" + ident
            if self.fail_create == "before":
                raise CloudError("connection reset")
            if name in self.agents:
                raise CloudError("409 ALREADY_EXISTS")
            now = self.tick()
            self.agents[name] = {"name": name, "displayName": body["displayName"], "description": body["description"],
                                 "createTime": now, "updateTime": now, "dataAnalyticsAgent": copy.deepcopy(body["dataAnalyticsAgent"])}
            if self.fail_create == "after":
                raise LostResponse("response lost")
            return copy.deepcopy(self.agents[name])
        if method == "DELETE":
            name = path.removesuffix(":deleteSync")
            now = self.tick()
            self.agents[name].update(updateTime=now, deleteTime=now, purgeTime="2026-12-01T00:00:00Z")
            return {}
        raise AssertionError(method)

    def methods(self):
        return [method for method, _ in self.calls]


class DeletedAgentReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.cfg = {"project_id": "participant-project", "dataset_id": "ec_workshop", "data_agent_location": "us",
                    "bigquery_location": "US"}
        self.client = AgentClient(self.cfg)
        self.client.folder = self.folder
        self.cloud = FakeService(self.client)
        self.client.request = self.cloud.request
        self.ctx = context(self.cfg, "raw")
        self.ids = candidate_ids("raw", fingerprint(self.ctx))
        self.names = [self.client.parent + "/dataAgents/" + i for i in self.ids]

    def ledger(self, index):
        path = self.folder / (self.ids[index] + ".resource.json")
        return json.loads(path.read_text()) if path.is_file() else None

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.folder.glob("*.json")}

    def assert_stops_without_change(self, pattern):
        before, agents, calls = self.snapshot(), copy.deepcopy(self.cloud.agents), len(self.cloud.calls)
        with self.assertRaisesRegex(CloudError, pattern):
            self.client.ensure("raw", self.ctx)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(agents, self.cloud.agents)
        self.assertNotIn("POST", self.cloud.methods()[calls:])

    def cleanup(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.client.teardown(execute=True)

    # --- 片付け → 再準備 ---

    def test_original_id_comes_first_and_is_reused_with_one_get(self):
        self.assertEqual(self.client.ensure("raw", self.ctx), self.names[0])
        self.assertIs(self.client.last_created, True)
        calls = len(self.cloud.calls)
        self.assertEqual(self.client.ensure("raw", self.ctx), self.names[0])
        self.assertIs(self.client.last_created, False)
        self.assertEqual(self.cloud.methods()[calls:], ["GET"])

    def test_cleanup_then_prepare_uses_the_next_id_until_the_limit(self):
        self.client.ensure("raw", self.ctx)
        for index in range(1, 5):
            self.cleanup()
            self.assertEqual(self.client.ensure("raw", self.ctx), self.names[index])
            self.assertTrue(self.ids[index].endswith("-r%d" % (index + 1)))
            self.assertEqual(self.ledger(index)["state"], "present")
            self.assertEqual(self.ledger(index - 1)["state"], "deleted")
        self.cleanup()
        self.assert_stops_without_change("重ならないIDが残っていません")

    def test_purged_original_id_is_used_again(self):
        self.client.ensure("raw", self.ctx)
        self.cleanup()
        del self.cloud.agents[self.names[0]]  # 30 日後に完全に消えた
        self.assertEqual(self.client.ensure("raw", self.ctx), self.names[0])

    def test_cleanup_deletes_the_numbered_agent_and_skips_deleted_ledgers(self):
        self.client.ensure("raw", self.ctx)
        self.cleanup()
        self.client.ensure("raw", self.ctx)
        calls = len(self.cloud.calls)
        self.assertEqual(self.cleanup(), 1)
        self.assertEqual(self.cloud.calls[calls:], [("GET", self.client.base + self.names[1]),
                                                    ("DELETE", self.client.base + self.names[1] + ":deleteSync")])
        self.assertEqual(self.ledger(1)["state"], "deleted")

    # --- 作成の途中で止まった場合：次の ID へ進まず、重複して作らず、止まる ---

    def interrupted(self, when, index):
        self.cloud.fail_create = when
        with self.assertRaises(CloudError):
            self.client.ensure("raw", self.ctx)
        self.cloud.fail_create = None
        self.assertEqual(self.ledger(index)["state"], "creation_requested")
        self.assertEqual(self.names[index] in self.cloud.agents, when == "after")
        self.assert_stops_without_change("作成の途中で止まった")
        self.assertEqual([n for n in self.cloud.agents if "deleteTime" not in self.cloud.agents[n]],
                         [self.names[index]] if when == "after" else [])

    def test_interrupted_creation_before_the_request_stops(self):
        self.interrupted("before", 0)

    def test_interrupted_creation_after_the_request_stops(self):
        self.interrupted("after", 0)

    def test_interrupted_creation_of_a_numbered_id_stops(self):
        for when in ("before", "after"):
            with self.subTest(when=when):
                for path in self.folder.glob("*"):
                    path.unlink()
                self.cloud.agents.clear()
                self.client.ensure("raw", self.ctx)
                self.cleanup()
                self.interrupted(when, 1)

    # --- 削除済みの判定は deleteTime だけ ---

    def test_deletion_state_needs_a_delete_time(self):
        self.assertEqual(deletion_state(None), "absent")
        self.assertEqual(deletion_state({"name": "a"}), "usable")
        self.assertEqual(deletion_state({"deleteTime": "2026-10-31T07:52:52Z", "purgeTime": "x"}), "deleted")
        for odd in ({"purgeTime": "x"}, {"deleteTime": ""}, {"deleteTime": None}, {"deleteTime": 1}):
            self.assertEqual(deletion_state(odd), "unknown")

    def test_deleted_ledger_with_a_usable_agent_stops(self):
        self.client.ensure("raw", self.ctx)
        record = self.ledger(0)
        record["state"] = "deleted"
        (self.folder / (self.ids[0] + ".resource.json")).write_text(json.dumps(record))
        self.assert_stops_without_change("削除済みですが、Agentが使える状態")

    def test_unknown_deletion_shapes_stop(self):
        self.client.ensure("raw", self.ctx)
        self.cleanup()
        for odd in ({"purgeTime": "2026-12-01T00:00:00Z"}, {"deleteTime": ""}):
            with self.subTest(odd=odd):
                agent = self.cloud.agents[self.names[0]]
                agent.pop("deleteTime", None)
                agent.pop("purgeTime", None)
                agent.update(odd)
                self.assert_stops_without_change("削除状態を判定できません")

    def test_manually_deleted_agent_with_present_ledger_stops_and_keeps_the_ledger(self):
        self.client.ensure("raw", self.ctx)
        self.cloud.request("DELETE", self.client.base + self.names[0] + ":deleteSync")  # Console などから削除
        self.assert_stops_without_change("教材の片付け以外で削除")
        self.assertEqual(self.ledger(0)["state"], "present")

    # --- 通常の不一致では次の ID を使わない ---

    def test_ordinary_mismatches_never_move_to_the_next_id(self):
        self.client.ensure("raw", self.ctx)
        self.cloud.agents[self.names[0]]["description"] = "changed"
        self.assert_stops_without_change("Existing Agent changed")
        self.cloud.agents[self.names[0]]["description"] = self.ledger(0)["description"]
        self.cloud.agents[self.names[0]]["createTime"] = "2026-12-31T00:00:00Z"
        self.assert_stops_without_change("Agent台帳の不足")

    def test_missing_ledger_with_a_usable_agent_stops(self):
        self.client.ensure("raw", self.ctx)
        (self.folder / (self.ids[0] + ".resource.json")).unlink()
        self.assert_stops_without_change("Agent台帳の不足")

    def test_recorded_agent_that_disappeared_stops(self):
        self.client.ensure("raw", self.ctx)
        del self.cloud.agents[self.names[0]]
        self.assert_stops_without_change("記録済みAgentが見つかりません")

    def test_two_pinned_ledgers_stop(self):
        self.client.ensure("raw", self.ctx)
        (self.folder / (self.ids[1] + ".resource.json")).write_text(json.dumps({"name": self.names[1], "state": "creation_requested"}))
        self.assert_stops_without_change("Agent台帳が複数あります")

    def test_two_unrecorded_usable_agents_stop(self):
        self.client.ensure("raw", self.ctx)
        self.cleanup()
        self.client.ensure("raw", self.ctx)
        self.cloud.agents[self.names[2]] = dict(copy.deepcopy(self.cloud.agents[self.names[1]]), name=self.names[2])
        for path in self.folder.glob("*.resource.json"):
            path.unlink()
        self.assert_stops_without_change("台帳のないAgentが複数あります")

    def test_resolve_only_reads(self):
        self.client.ensure("raw", self.ctx)
        self.cleanup()
        before = self.snapshot()
        decision = self.client.resolve("raw", self.ctx)
        self.assertEqual((decision["action"], decision["name"]), ("create", self.names[1]))
        self.assertEqual(set(decision["probed"]), set(self.names))
        self.assertEqual(before, self.snapshot())
        self.assertNotIn("POST", self.cloud.methods()[-5:])


if __name__ == "__main__":
    unittest.main()

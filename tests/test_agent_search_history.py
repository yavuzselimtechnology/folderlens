import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentarm.mcp_server import create_agent
from agentarm.search_history import SearchHistory
from agentarm.workspace import AgentPolicy, Workspace


class SearchHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "many.txt").write_text("needle\n" * 130, encoding="utf-8")
        self.workspace = Workspace(AgentPolicy(self.root))

    def test_replay_tail_and_discovery_are_independent_snapshots(self):
        first = self.workspace.start_search("needle")
        key = first["search_id"]
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["history_end"], 50)
        (self.root / "many.txt").write_text("changed source")
        replay = self.workspace.more_search(key, limit=3, offset=0)
        self.assertEqual(replay["results"], first["results"][:3])
        self.assertEqual(replay["matched"], first["matched"])
        self.assertEqual(replay["scanned"], first["scanned"])
        tail = self.workspace.more_search(key, limit=2, offset=-3)
        self.assertEqual([x["line"] for x in tail["results"]], [48, 49])
        self.assertEqual(tail["offset"], 47)
        self.assertEqual(tail["next_offset"], 49)
        last = self.workspace.more_search(key, offset=-1)
        self.assertFalse(last["has_more"])
        self.assertTrue(last["search_has_more"])
        following = self.workspace.more_search(key, limit=7)
        self.assertEqual([x["line"] for x in following["results"]], list(range(51, 58)))
        self.assertEqual(following["offset"], 50)
        self.assertEqual(following["delivery"], "discovery")
        self.assertFalse(self.workspace.more_search(key, offset=99999)["results"])

    def test_eviction_reports_available_span_and_keeps_scanning_lossless(self):
        with patch(
            "agentarm.workspace.SearchHistory", side_effect=lambda: SearchHistory(800)
        ):
            first = self.workspace.start_search("needle")
        key = first["search_id"]
        self.assertGreater(first["history_start"], 0)
        self.assertLessEqual(first["history_bytes"], 800)
        replay = self.workspace.more_search(key, offset=0)
        self.assertTrue(replay["requested_results_dropped"])
        self.assertEqual(replay["offset"], replay["history_start"])
        self.assertEqual(replay["results"][0]["line"], replay["offset"] + 1)
        tail = self.workspace.more_search(key, offset=-1)
        self.assertFalse(tail["requested_results_dropped"])
        self.assertEqual(tail["results"][0]["line"], 50)
        results = list(first["results"])
        page = first
        while page["has_more"]:
            page = self.workspace.more_search(key)
            results.extend(page["results"])
        self.assertEqual([x["line"] for x in results], list(range(1, 131)))

    def test_serialized_snapshot_is_immutable_and_oversized_record_can_be_dropped(self):
        history = SearchHistory(100)
        row = {"path": "Türkçe", "text": "old"}
        history.append([row])
        row["text"] = "external mutation"
        first = history.page(0, 1, 100)
        self.assertEqual(first["results"][0]["text"], "old")
        first["results"][0]["text"] = "read mutation"
        self.assertEqual(history.page(0, 1, 100)["results"][0]["text"], "old")
        history.append([{"text": "large" * 100}])
        self.assertEqual(history.start, history.end)
        self.assertEqual(history.bytes, 0)
        self.assertTrue(history.page(0, 1, 100)["requested_results_dropped"])
        for budget in (0, -1, True):
            with self.assertRaises(ValueError):
                SearchHistory(budget)

    def test_stop_expiry_and_invalid_windows_release_or_preserve_sessions(self):
        first = self.workspace.start_search("needle")
        key = first["search_id"]
        for args in ({"limit": 0}, {"limit": True}, {"offset": True}, {"offset": 1.5}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.workspace.more_search(key, **args)
        self.workspace.searches[key]["touched"] -= self.workspace.policy.search_ttl + 1
        with self.assertRaises(KeyError):
            self.workspace.more_search(key, offset=0)
        second = self.workspace.start_search("needle")
        self.workspace.stop_search(second["search_id"])
        self.assertFalse(self.workspace.searches)

    def test_mcp_history_schema_and_catalog_metadata(self):
        async def run():
            agent = create_agent(self.workspace.policy)
            started = await agent.call_tool("start_search", {"pattern": "needle"})
            self.assertFalse(started.is_error)
            first = json.loads(started.content[0].text)
            result = await agent.call_tool(
                "get_more_search_results",
                {"search_id": first["search_id"], "offset": -2, "limit": 2},
            )
            self.assertFalse(result.is_error)
            page = json.loads(result.content[0].text)
            self.assertEqual(page["results"], first["results"][-2:])
            listed = await agent.call_tool("list_searches", {})
            self.assertEqual(
                json.loads(listed.content[0].text)["searches"][0]["history_end"], 50
            )

        asyncio.run(run())

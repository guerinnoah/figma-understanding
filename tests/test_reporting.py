import json
import subprocess
import sys
import tempfile
import unittest
import hashlib
from pathlib import Path

from figma_understanding.acquire import acquire_visuals
from figma_understanding.manifest import sha256_file
from figma_understanding.observations import record_observation
from figma_understanding.reporting import build_frontend_handoff, _one_line
from tests.helpers import png


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "figma_tool.py"


class ReportingTests(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args], cwd=ROOT, text=True, capture_output=True)

    def make_live_workspace(self, workspace):
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"]["file_key"] = "LiveKey123"
        manifest["source"]["url"] = "https://www.figma.com/design/LiveKey123/Live"
        manifest["source"].pop("synthetic", None)
        for request in manifest["requests"].values():
            request["endpoint"] = request["endpoint"].replace("demoKey123", "LiveKey123")
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def acquire_demo_visuals(self, workspace, *, roles=None, unit_ids=None):
        class Client:
            def render_urls(self, file_key, node_ids, *, version, batch_size=50):
                return {node_id: f"https://images.figma.com/{node_id}.png" for node_id in node_ids}

            def original_urls(self, file_key):
                return {}

        body = png()

        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "width": 2,
                "height": 3,
                "content_type": "image/png",
            }

        raw = json.loads((workspace / "raw" / "file.json").read_text())
        reviewed = json.loads((workspace / "render-plan.json").read_text())
        inventory = json.loads((workspace / "manifest.json").read_text())
        return acquire_visuals(
            file_key=inventory["source"]["file_key"],
            design_version=raw["version"],
            workspace=workspace,
            client=Client(),
            downloader=downloader,
            plan=reviewed,
            inventory_provenance={
                "inventory_manifest_sha256": sha256_file(workspace / "manifest.json"),
                "raw_file_sha256": sha256_file(workspace / "raw" / "file.json"),
                "plan_sha256": sha256_file(workspace / "render-plan.json"),
            },
            roles=roles,
            unit_ids=unit_ids,
        )

    def test_demo_status_recommends_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            self.assertEqual(0, result.returncode)
            value = json.loads(result.stdout)
            self.assertTrue(value["inventory"]["valid"])
            self.assertTrue(value["plan"]["current"])
            self.assertIn("handoff", value["next_command"])

    def test_originals_enabled_status_command_preserves_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "live"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            self.make_live_workspace(workspace)
            plan = self.run_tool(
                "plan",
                "--workspace",
                str(workspace),
                "--include-originals",
            )
            self.assertEqual(0, plan.returncode, plan.stderr)

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads(result.stdout)

        self.assertIn("acquire", value["next_command"])
        self.assertIn("--include-originals", value["next_command"])

    def test_visual_manifest_contains_only_asset_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            visual = self.acquire_demo_visuals(workspace, unit_ids={"render:1-1"})

        self.assertNotIn("plan", visual)
        asset = next(item for item in visual["assets"] if item["unit_id"] == "render:1-1")
        self.assertNotIn("source_url", asset)
        self.assertNotIn("final_url", asset)
        self.assertNotIn("inspected", asset["status"])

    def test_status_recommends_remaining_explicitly_selected_units(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            plan_result = self.run_tool(
                "plan",
                "--workspace",
                str(workspace),
                "--node-id",
                "1:2",
            )
            self.assertEqual(0, plan_result.returncode, plan_result.stderr)
            visual = self.acquire_demo_visuals(workspace, unit_ids={"render:1-1"})
            record_observation(workspace, "render:1-1", {"layout": ["Checkout context"]})

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads(result.stdout)

        self.assertIn("remaining units", value["next_action"])
        self.assertIn("--unit-id render:1-2", value["next_command"])
        pending = next(asset for asset in visual["assets"] if asset["unit_id"] == "render:1-2")
        self.assertFalse(pending["status"]["downloaded"])

    def test_status_rejects_malformed_or_mismatched_visual_manifest(self):
        def null_source(visual):
            visual["source"] = None

        def empty_assets(visual):
            visual["assets"] = []

        def mismatched_assets(visual):
            visual["assets"][0]["unit_id"] = "render:unexpected"

        def null_status(visual):
            visual["assets"][0]["status"] = None

        def empty_manifest(visual):
            visual.clear()

        for name, mutate in (
            ("null source", null_source),
            ("empty assets", empty_assets),
            ("mismatched assets", mismatched_assets),
            ("null status", null_status),
            ("empty manifest", empty_manifest),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "demo"
                self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
                self.acquire_demo_visuals(workspace, unit_ids={"render:1-1"})
                visual_path = workspace / "visual-manifest.json"
                visual = json.loads(visual_path.read_text(encoding="utf-8"))
                mutate(visual)
                visual_path.write_text(json.dumps(visual), encoding="utf-8")

                result = self.run_tool("status", "--workspace", str(workspace), "--json")
                self.assertEqual(0, result.returncode, result.stderr)
                value = json.loads(result.stdout)

                self.assertFalse(value["visuals"]["bound"])
                self.assertTrue(value["visuals"]["errors"])

    def test_detail_only_unscoped_status_does_not_skip_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "live"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            self.make_live_workspace(workspace)
            normalized_path = workspace / "normalized.json"
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            normalized["threads"] = []
            normalized_path.write_text(
                json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["normalized.json"]["sha256"] = sha256_file(normalized_path)
            manifest["counts"]["threads"] = 0
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)
            plan_path = workspace / "render-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            detail = next(unit for unit in plan["units"] if unit["roles"] == ["detail"])
            plan["units"] = [detail]
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            visual = self.acquire_demo_visuals(workspace, roles={"overview"})
            self.assertEqual(0, visual["summary"]["downloaded"])

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads(result.stdout)

        self.assertIn("acquire", value["next_command"])
        self.assertNotIn("handoff", value["next_command"])

    def test_status_routes_unsupported_plan_to_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            plan_path = workspace / "render-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["schema"] = "unsupported"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            value = json.loads(result.stdout)
            self.assertFalse(value["plan"]["current"])
            self.assertTrue(value["next_command"].endswith(" plan --workspace " + str(workspace)))

    def test_status_routes_stale_plan_and_modified_asset_to_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            plan_path = workspace / "render-plan.json"
            plan = json.loads(plan_path.read_text())
            plan["normalized_sha256"] = "stale"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            value = json.loads(self.run_tool("status", "--workspace", str(workspace), "--json").stdout)
            self.assertFalse(value["plan"]["current"])
            self.assertIn(" plan --workspace ", value["next_command"])

            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)
            visual = self.acquire_demo_visuals(workspace)
            asset = next(item for item in visual["assets"] if item["status"]["downloaded"])
            (workspace / asset["local_path"]).write_bytes(b"modified")
            value = json.loads(self.run_tool("status", "--workspace", str(workspace), "--json").stdout)
            self.assertEqual([asset["unit_id"]], value["invalid_visual_units"])
            self.assertIn("Reacquire", value["next_action"])

    def test_handoff_excludes_modified_visual_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            visual = self.acquire_demo_visuals(workspace)
            asset = next(item for item in visual["assets"] if item["unit_id"] == "render:1-1")
            (workspace / asset["local_path"]).write_bytes(b"modified")
            handoff = build_frontend_handoff(workspace)
            self.assertFalse(any(
                item["unit_id"] == asset["unit_id"]
                for item in handoff["evidence_assets"]
            ))
            self.assertIsNone(handoff["open_decisions"][0]["evidence"]["local_path"])
            self.assertTrue(any("missing or modified" in item for item in handoff["limitations"]))

    def test_handoff_excludes_nodes_hidden_by_an_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            normalized_path = workspace / "normalized.json"
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            for node in normalized["nodes"]:
                if node["id"] in {"1:1", "1:2"}:
                    node["visible"] = False
            normalized_path.write_text(
                json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["normalized.json"]["sha256"] = sha256_file(normalized_path)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)

            handoff = build_frontend_handoff(workspace)

        self.assertFalse(any(surface["node_id"] == "1:1" for surface in handoff["surfaces"]))
        self.assertFalse(any(item["text"] == "Checkout" for item in handoff["copy_inventory"]))

    def test_handoff_includes_top_level_instance_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            normalized_path = workspace / "normalized.json"
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            next(node for node in normalized["nodes"] if node["id"] == "1:1")["type"] = "INSTANCE"
            normalized_path.write_text(
                json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["normalized.json"]["sha256"] = sha256_file(normalized_path)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)

            handoff = build_frontend_handoff(workspace)

        self.assertTrue(any(surface["node_id"] == "1:1" for surface in handoff["surfaces"]))

    def test_markdown_source_text_is_escaped(self):
        self.assertEqual(r"\*bold\* \[link\]", _one_line("*bold* [link]"))

    def test_handoff_writes_structured_and_readable_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            output = Path(directory) / "handoff"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)

            result = self.run_tool("handoff", "--workspace", str(workspace), "--output-dir", str(output))
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads((output / "frontend-handoff.json").read_text(encoding="utf-8"))
            markdown = (output / "frontend-handoff.md").read_text(encoding="utf-8")
            self.assertEqual("frontend-handoff/v1", value["schema"])
            self.assertEqual(1, value["coverage"]["comment_threads"])
            self.assertEqual(0, value["coverage"]["observed_visual_units"])
            self.assertEqual("c1", value["open_decisions"][0]["thread_id"])
            self.assertIn("resolved_threads", value)
            self.assertNotIn("resolved_decisions", value)
            self.assertTrue(any(item["text"] == "Checkout" for item in value["copy_inventory"]))
            checkout = next(item for item in value["surfaces"] if item["node_id"] == "1:1")
            self.assertEqual("VERTICAL", checkout["implementation"]["layout"]["mode"])
            self.assertEqual("Heading/Large", value["design_system"]["styles"]["style:heading"]["name"])
            self.assertIn("## Design system evidence", markdown)
            self.assertIn("## Visual observations", markdown)
            self.assertIn("## Open decisions", markdown)
            self.assertIn("## Resolved threads", markdown)
            self.assertIn("Can we clarify this?", markdown)


if __name__ == "__main__":
    unittest.main()

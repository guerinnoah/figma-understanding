import json
import subprocess
import sys
import tempfile
import unittest
import hashlib
from pathlib import Path

from figma_understanding.acquire import acquire_visuals
from figma_understanding.inventory import normalize
from figma_understanding.manifest import sha256_file
from figma_understanding.observations import record_observation
from figma_understanding.reporting import (
    _authored_label,
    _build_annotations,
    _implementation_unknowns,
    _one_line,
    _scope_handoff,
    build_frontend_handoff,
    frontend_handoff_markdown,
)
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
            self.assertEqual(0, value["plan"]["flows"])
            self.assertEqual(0, value["plan"]["unresolved_connectors"])
            self.assertIn("handoff", value["next_command"])

    def test_empty_prototype_flow_keeps_missing_transition_warning(self):
        unknowns = _implementation_unknowns(
            [{
                "implementation": {
                    "prototype": {"flow_starting_points": [{"nodeId": "A"}]},
                },
            }],
            [],
            [{"kind": "prototype", "edges": []}],
        )

        self.assertTrue(any(
            "No prototype transitions were captured" in item
            for item in unknowns
        ))

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

    def test_detail_only_status_recommends_detail_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "live"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            self.make_live_workspace(workspace)
            raw_path = workspace / "raw" / "file.json"
            comments_path = workspace / "raw" / "comments.json"
            normalized_path = workspace / "normalized.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw["document"]["children"][0]["children"] = [{
                "id": "detail:1",
                "type": "STICKY",
                "name": "Only detail",
                "characters": "Review this detail",
            }]
            comments = {"comments": []}
            normalized = normalize(raw, comments)
            raw_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            comments_path.write_text(
                json.dumps(comments, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            normalized_path.write_text(
                json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["raw/file.json"]["sha256"] = sha256_file(raw_path)
            manifest["artifacts"]["raw/comments.json"]["sha256"] = sha256_file(comments_path)
            manifest["artifacts"]["normalized.json"]["sha256"] = sha256_file(normalized_path)
            manifest["counts"] = {
                "nodes": len(normalized["nodes"]),
                "threads": len(normalized["threads"]),
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads(result.stdout)

        self.assertIn("detail evidence", value["next_action"])
        self.assertTrue(value["next_command"].endswith("--role detail"))

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

    def test_status_names_visual_coverage_gaps_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            plan = json.loads((workspace / "render-plan.json").read_text())
            unit_id = plan["units"][0]["unit_id"]
            self.acquire_demo_visuals(workspace, unit_ids={unit_id})

            result = self.run_tool("status", "--workspace", str(workspace), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads(result.stdout)

        self.assertEqual(
            len(plan["units"]) - 1,
            value["visuals"]["coverage"]["planned_not_downloaded"],
        )
        self.assertEqual(
            1,
            value["observations"]["downloaded_without_observations"],
        )
        self.assertNotIn("missing", value["observations"])

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
            self.assertIsNone(handoff["unresolved_threads"][0]["evidence"]["local_path"])
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

    def test_handoff_includes_unknown_top_level_overview_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            raw_path = workspace / "raw" / "file.json"
            normalized_path = workspace / "normalized.json"
            manifest_path = workspace / "manifest.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            raw["document"]["children"][0]["children"][0]["type"] = "FUTURE_SURFACE"
            next(node for node in normalized["nodes"] if node["id"] == "1:1")["type"] = "FUTURE_SURFACE"
            raw_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            normalized_path.write_text(
                json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["raw/file.json"]["sha256"] = sha256_file(raw_path)
            manifest["artifacts"]["normalized.json"]["sha256"] = sha256_file(normalized_path)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)

            handoff = build_frontend_handoff(workspace)

        surface = next(item for item in handoff["surfaces"] if item["node_id"] == "1:1")
        self.assertEqual("FUTURE_SURFACE", surface["type"])
        self.assertEqual(["overview"], surface["visual_units"][0]["roles"])

    def test_handoff_reports_ordered_flow_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            raw_path = workspace / "raw" / "file.json"
            normalized_path = workspace / "normalized.json"
            manifest_path = workspace / "manifest.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            raw_nodes = raw["document"]["children"][0]["children"]
            frame_children = next(node for node in raw_nodes if node["id"] == "1:1")["children"]
            attached = next(node for node in frame_children if node["id"] == "1:3")
            attached.update({
                "type": "CONNECTOR",
                "connectorStart": {"endpointNodeId": "1:1"},
                "connectorEnd": {"endpointNodeId": "1:2"},
                "connectorEndStrokeCap": "LINE_ARROW",
                "connectorLineType": "ELBOWED",
            })
            next(node for node in raw_nodes if node["id"] == "2:1")["type"] = "CONNECTOR"
            raw_path.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            next(node for node in normalized["nodes"] if node["id"] == "1:3")["type"] = "CONNECTOR"
            next(node for node in normalized["nodes"] if node["id"] == "2:1")["type"] = "CONNECTOR"
            normalized_path.write_text(
                json.dumps(normalized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["raw/file.json"]["sha256"] = sha256_file(raw_path)
            manifest["artifacts"]["normalized.json"]["sha256"] = sha256_file(normalized_path)
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(0, self.run_tool("plan", "--workspace", str(workspace)).returncode)

            handoff = build_frontend_handoff(workspace)
            markdown = frontend_handoff_markdown(handoff)

        self.assertEqual("Checkout diagram", handoff["flows"][0]["name"])
        self.assertTrue(any("diagram order" in item for item in handoff["limitations"]))
        self.assertTrue(any(
            "No prototype transitions" in item
            for item in handoff["implementation_unknowns"]
        ))
        self.assertTrue(any("do not establish order" in item for item in handoff["limitations"]))
        self.assertIn("## Flow order", markdown)
        self.assertIn("Step 2: `1:2` Title", markdown)
        self.assertIn("`1:1` → `1:2` via `1:3` (ELBOWED)", markdown)
        self.assertIn("Unresolved connector `2:1`", markdown)
        self.assertIn("unattached-endpoint", markdown)
        self.assertNotIn("<unnamed>", markdown)

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
            self.assertEqual("frontend-handoff/v2", value["schema"])
            self.assertEqual(1, value["coverage"]["comment_threads"])
            self.assertEqual(0, value["coverage"]["observed_visual_units"])
            self.assertEqual("c1", value["unresolved_threads"][0]["thread_id"])
            self.assertEqual("question", value["unresolved_threads"][0]["authored_label"])
            self.assertEqual(
                "unknown",
                value["unresolved_threads"][0]["implementation_approval"],
            )
            self.assertIn("resolved_threads", value)
            self.assertNotIn("resolved_decisions", value)
            self.assertTrue(any(item["text"] == "Checkout" for item in value["copy_inventory"]))
            checkout = next(item for item in value["surfaces"] if item["node_id"] == "1:1")
            self.assertEqual("VERTICAL", checkout["implementation"]["layout"]["mode"])
            self.assertEqual("Heading/Large", value["design_system"]["styles"]["style:heading"]["name"])
            self.assertIn("## Design system evidence", markdown)
            self.assertIn("## Visual observations", markdown)
            self.assertIn("## Unresolved threads", markdown)
            self.assertIn("implementation approval: unknown", markdown)
            self.assertIn("## Resolved threads", markdown)
            self.assertIn("Can we clarify this?", markdown)

    def test_handoff_can_be_scoped_to_selected_visual_units(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            output = Path(directory) / "focused"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            plan = json.loads((workspace / "render-plan.json").read_text())
            linked = next(
                link
                for link in plan["comment_links"]
                if link.get("status") == "linked"
            )

            result = self.run_tool(
                "handoff",
                "--workspace",
                str(workspace),
                "--output-dir",
                str(output),
                "--unit-id",
                linked["unit_id"],
            )

            self.assertEqual(0, result.returncode, result.stderr)
            value = json.loads((output / "frontend-handoff.json").read_text())
            self.assertEqual(1, value["coverage"]["planned_visual_units"])
            self.assertEqual([linked["unit_id"]], value["source"]["handoff_scope"]["unit_ids"])
            self.assertEqual(1, value["coverage"]["comment_threads"])
            self.assertTrue(all(
                asset["unit_id"] == linked["unit_id"]
                for asset in value["evidence_assets"]
            ))

    def test_scope_uses_covering_units_and_ancestor_flow_context(self):
        normalized = {
            "threads": [{"thread_id": "t1", "node_id": "comment-node"}],
        }
        plan = {
            "units": [
                {
                    "unit_id": "render:selected",
                    "kind": "render",
                    "roles": ["detail"],
                    "node_id": "detail-node",
                    "coverage_node_ids": ["detail-node", "comment-node"],
                    "context_path": [{"id": "page"}, {"id": "screen-a"}],
                },
                {
                    "unit_id": "render:comment",
                    "kind": "render",
                    "roles": ["detail"],
                    "node_id": "comment-node",
                    "coverage_node_ids": ["comment-node"],
                    "context_path": [{"id": "page"}, {"id": "screen-a"}],
                },
            ],
            "comment_links": [{
                "thread_id": "t1",
                "node_id": "comment-node",
                "status": "linked",
                "strategy": "exact-node",
                "unit_id": "render:comment",
                "candidate_unit_ids": ["render:comment", "render:selected"],
            }],
            "flows": [
                {
                    "flow_id": "related",
                    "scope_node_ids": ["screen-a", "screen-b"],
                    "steps": [],
                },
                {
                    "flow_id": "unrelated",
                    "scope_node_ids": ["screen-x", "screen-y"],
                    "steps": [],
                },
            ],
            "unresolved_connectors": [
                {"connector_node_id": "c1", "start_node_id": "screen-b", "end_node_id": None},
                {"connector_node_id": "c2", "start_node_id": "screen-x", "end_node_id": None},
            ],
        }

        scoped_normalized, scoped_plan, node_ids = _scope_handoff(
            normalized,
            plan,
            {"render:selected"},
        )

        self.assertEqual(["t1"], [thread["thread_id"] for thread in scoped_normalized["threads"]])
        self.assertEqual("render:selected", scoped_plan["comment_links"][0]["scope_unit_id"])
        self.assertEqual(["related"], [flow["flow_id"] for flow in scoped_plan["flows"]])
        self.assertEqual(["c1"], [item["connector_node_id"] for item in scoped_plan["unresolved_connectors"]])
        self.assertTrue({"screen-a", "screen-b"}.issubset(node_ids))

    def test_authored_labels_do_not_imply_approval(self):
        self.assertEqual("suggestion", _authored_label("Suggestions: move this later"))
        self.assertEqual("question", _authored_label("Should this move later?"))
        self.assertEqual("implementation-request", _authored_label("Implement: move this later"))
        self.assertEqual("unlabelled", _authored_label("Move this later"))

        annotations = _build_annotations(
            [{
                "id": "sticky:1",
                "type": "STICKY",
                "characters": "Suggestions: move this later",
            }],
            {"units": [{"unit_id": "render:sticky-1", "node_id": "sticky:1", "roles": ["detail"]}]},
            {},
            set(),
        )
        self.assertEqual("suggestion", annotations[0]["authored_label"])
        self.assertEqual("unknown", annotations[0]["implementation_approval"])

    def test_figma_annotations_use_covering_visual_evidence(self):
        annotations = _build_annotations(
            [
                {
                    "id": "frame:1",
                    "type": "FRAME",
                    "annotations": [{
                        "label": "Implement: keep this width",
                        "properties": [{"type": "width"}],
                    }],
                },
                {
                    "id": "sticky:1",
                    "type": "STICKY",
                    "characters": "Suggestion: simplify this",
                },
            ],
            {"units": [{
                "unit_id": "render:overview",
                "kind": "render",
                "node_id": "section:1",
                "roles": ["overview"],
                "coverage_node_ids": ["section:1", "frame:1", "sticky:1"],
            }]},
            {"render:overview": {"local_path": "visuals/overview.png"}},
            {"render:overview"},
        )

        by_source = {item["source_type"]: item for item in annotations}
        figma_annotation = by_source["figma-annotation"]
        self.assertEqual("Implement: keep this width", figma_annotation["text"])
        self.assertEqual(["width"], figma_annotation["pinned_properties"])
        self.assertEqual("implementation-request", figma_annotation["authored_label"])
        self.assertEqual("render:overview", figma_annotation["evidence"]["unit_id"])
        self.assertTrue(figma_annotation["evidence"]["inspected"])
        self.assertEqual("render:overview", by_source["sticky"]["evidence"]["unit_id"])


if __name__ == "__main__":
    unittest.main()

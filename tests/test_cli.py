import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from figma_understanding.acquire import acquire_visuals
from figma_understanding.cli import _acquisition_result
from figma_understanding.manifest import sha256_file
from tests.helpers import png


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "figma_tool.py"


class CLITests(unittest.TestCase):
    def test_acquisition_result_separates_current_request_from_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            manifest = self.acquire_demo_visuals(workspace)
            plan = json.loads((workspace / "render-plan.json").read_text())
            selected = {unit["unit_id"] for unit in plan["units"][:2]}
            reusable = {next(iter(selected))}

            result = _acquisition_result(
                workspace,
                plan,
                manifest,
                selected,
                reusable,
            )

        self.assertEqual(2, result["requested"])
        self.assertEqual(1, result["downloaded_now"])
        self.assertEqual(1, result["reused"])
        self.assertEqual(0, result["failed_now"])
        self.assertEqual(manifest["summary"]["downloaded"], result["total_downloaded"])

    def run_tool(self, *args, env=None):
        environment = os.environ.copy()
        environment.pop("FIGMA_TOKEN", None)
        environment.update(env or {})
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )

    def acquire_demo_visuals(self, workspace):
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
        plan = json.loads((workspace / "render-plan.json").read_text())
        inventory = json.loads((workspace / "manifest.json").read_text())
        return acquire_visuals(
            file_key=inventory["source"]["file_key"],
            design_version=raw["version"],
            workspace=workspace,
            client=Client(),
            downloader=downloader,
            plan=plan,
            inventory_provenance={
                "inventory_manifest_sha256": sha256_file(workspace / "manifest.json"),
                "raw_file_sha256": sha256_file(workspace / "raw" / "file.json"),
                "plan_sha256": sha256_file(workspace / "render-plan.json"),
            },
        )

    def test_internal_inspection_marker_is_not_a_public_command(self):
        result = self.run_tool("--help")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("mark-inspected", result.stdout)

    def test_removed_scope_and_file_key_options_are_not_public(self):
        ingest_help = self.run_tool("ingest", "--help")
        acquire_help = self.run_tool("acquire", "--help")
        self.assertEqual(0, ingest_help.returncode, ingest_help.stderr)
        self.assertEqual(0, acquire_help.returncode, acquire_help.stderr)
        self.assertNotIn("--scope", ingest_help.stdout)
        self.assertNotIn("--file-key", acquire_help.stdout)

    def test_invalid_ingest_url_has_clean_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_tool(
                "ingest",
                "--url",
                "not-a-figma-url",
                "--workspace",
                str(Path(directory) / "snapshot"),
                env={"FIGMA_TOKEN": "test-token"},
            )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("expected an HTTPS figma.com file URL", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_plan_rejects_tampered_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            (workspace / "raw" / "file.json").write_text("{}", encoding="utf-8")
            result = self.run_tool("plan", "--workspace", str(workspace))
            self.assertNotEqual(0, result.returncode)
            self.assertIn("workspace integrity failed", result.stderr)

    def test_demo_plan_links_normalized_comments_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            plan = json.loads((workspace / "render-plan.json").read_text(encoding="utf-8"))
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual("visual-plan/v2", plan["schema"])
            self.assertEqual("complete", plan["comments_extraction_status"])
            self.assertEqual(manifest["artifacts"]["normalized.json"]["sha256"], plan["normalized_sha256"])
            link = next(item for item in plan["comment_links"] if item["thread_id"] == "c1")
            self.assertEqual("render:1-1", link["unit_id"])
            unit = next(item for item in plan["units"] if item["unit_id"] == "render:1-1")
            self.assertEqual(["c1"], unit["thread_ids"])

    def test_plan_selection_flags_are_persisted_and_invalid_ids_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            result = self.run_tool(
                "plan",
                "--workspace",
                str(workspace),
                "--node-id",
                "1:2",
                "--page",
                "0:1",
                "--max-detail",
                "1",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            plan = json.loads((workspace / "render-plan.json").read_text())
            self.assertEqual(["1:2"], plan["selection"]["node_ids"])
            self.assertEqual(["0:1"], plan["selection"]["page_ids"])
            self.assertEqual(1, plan["selection"]["max_detail"])

            invalid = self.run_tool(
                "plan",
                "--workspace",
                str(workspace),
                "--node-id",
                "missing",
            )
            self.assertNotEqual(0, invalid.returncode)
            self.assertIn("unknown node", invalid.stderr)

    def test_plan_section_selector_is_applied_by_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            raw_path = workspace / "raw" / "file.json"
            normalized_path = workspace / "normalized.json"
            manifest_path = workspace / "manifest.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            raw["document"]["children"][0]["children"][0]["type"] = "SECTION"
            next(node for node in normalized["nodes"] if node["id"] == "1:1")["type"] = "SECTION"
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

            result = self.run_tool(
                "plan",
                "--workspace",
                str(workspace),
                "--section",
                "Checkout",
            )

            self.assertEqual(0, result.returncode, result.stderr)
            plan = json.loads((workspace / "render-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(["Checkout"], plan["selection"]["section_names"])
            self.assertEqual(["1:1"], plan["selection"]["resolved_section_node_ids"])
            self.assertEqual(
                {"render:1-1", "render:1-3"},
                {unit["unit_id"] for unit in plan["units"]},
            )

    def test_plan_rebuilds_flow_facts_from_raw_for_legacy_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            raw_path = workspace / "raw" / "file.json"
            normalized_path = workspace / "normalized.json"
            manifest_path = workspace / "manifest.json"
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            raw["document"]["children"][0]["children"].extend([
                {
                    "id": "3:1",
                    "type": "CONNECTOR",
                    "connectorStart": {"endpointNodeId": "1:1"},
                    "connectorEnd": {"endpointNodeId": "2:1"},
                    "connectorEndStrokeCap": "LINE_ARROW",
                },
                {
                    "id": "3:2",
                    "type": "CONNECTOR",
                    "connectorStart": {"endpointNodeId": "1:1"},
                },
            ])
            normalized["nodes"].extend([
                {
                    "id": "3:1",
                    "type": "CONNECTOR",
                    "name": "",
                    "parent_id": "0:1",
                    "page_id": "0:1",
                    "visible": True,
                    "implementation": {},
                },
                {
                    "id": "3:2",
                    "type": "CONNECTOR",
                    "name": "",
                    "parent_id": "0:1",
                    "page_id": "0:1",
                    "visible": True,
                    "implementation": {},
                },
            ])
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

            result = self.run_tool("plan", "--workspace", str(workspace))

            self.assertEqual(0, result.returncode, result.stderr)
            plan = json.loads((workspace / "render-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(["connector:3:1"], [flow["flow_id"] for flow in plan["flows"]])
            self.assertEqual("diagram-order", plan["flows"][0]["semantics"])

            plan["flows"][0]["edges"][0]["destination_node_id"] = "1:1"
            (workspace / "render-plan.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            acquire = self.run_tool("acquire", "--workspace", str(workspace))
            self.assertNotEqual(0, acquire.returncode)
            self.assertIn("raw Figma evidence and selection", acquire.stderr)

            self.assertEqual(
                0,
                self.run_tool("plan", "--workspace", str(workspace)).returncode,
            )
            plan = json.loads((workspace / "render-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(plan["unresolved_connectors"]))
            plan["unresolved_connectors"] = []
            (workspace / "render-plan.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            acquire = self.run_tool("acquire", "--workspace", str(workspace))
            self.assertNotEqual(0, acquire.returncode)
            self.assertIn("raw Figma evidence and selection", acquire.stderr)

            self.assertEqual(
                0,
                self.run_tool("plan", "--workspace", str(workspace)).returncode,
            )
            plan = json.loads((workspace / "render-plan.json").read_text(encoding="utf-8"))
            plan["flows"] = []
            (workspace / "render-plan.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            acquire = self.run_tool("acquire", "--workspace", str(workspace))
            self.assertNotEqual(0, acquire.returncode)
            self.assertIn("raw Figma evidence and selection", acquire.stderr)

    def test_original_opt_in_is_preserved_when_filter_selects_no_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            result = self.run_tool(
                "plan",
                "--workspace",
                str(workspace),
                "--include-originals",
                "--node-id",
                "1:2",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            plan = json.loads((workspace / "render-plan.json").read_text())
            self.assertTrue(plan["selection"]["include_originals"])
            self.assertFalse(any(unit["kind"] == "original" for unit in plan["units"]))

            acquire = self.run_tool(
                "acquire",
                "--workspace",
                str(workspace),
                "--include-originals",
            )
            self.assertIn("FIGMA_TOKEN is not set", acquire.stderr)
            self.assertNotIn("must match", acquire.stderr)

    def test_observe_rejects_visuals_bound_to_changed_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            visual = self.acquire_demo_visuals(workspace)
            unit_id = visual["assets"][0]["unit_id"]
            (workspace / "render-plan.json").write_text("{}", encoding="utf-8")
            result = self.run_tool(
                "observe",
                "--workspace",
                str(workspace),
                "--unit-id",
                unit_id,
                "--layout",
                "Vertical",
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("not bound", result.stderr)
            self.assertFalse((workspace / "observations.json").exists())

    def test_observe_validation_has_clean_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            visual = self.acquire_demo_visuals(workspace)
            unit_id = next(asset["unit_id"] for asset in visual["assets"] if asset["status"]["downloaded"])
            result = self.run_tool("observe", "--workspace", str(workspace), "--unit-id", unit_id)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("at least one observation is required", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_observe_rejects_modified_or_deleted_asset_without_changing_observations(self):
        for mutation in ("modified", "deleted"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "demo"
                self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
                visual = self.acquire_demo_visuals(workspace)
                asset = next(item for item in visual["assets"] if item["status"]["downloaded"])
                initial = self.run_tool(
                    "observe",
                    "--workspace",
                    str(workspace),
                    "--unit-id",
                    asset["unit_id"],
                    "--layout",
                    "Initial observation",
                )
                self.assertEqual(0, initial.returncode, initial.stderr)
                observations_path = workspace / "observations.json"
                before = observations_path.read_bytes()
                asset_path = workspace / asset["local_path"]
                if mutation == "modified":
                    asset_path.write_bytes(b"modified")
                else:
                    asset_path.unlink()

                result = self.run_tool(
                    "observe",
                    "--workspace",
                    str(workspace),
                    "--unit-id",
                    asset["unit_id"],
                    "--layout",
                    "Replacement observation",
                )

                self.assertNotEqual(0, result.returncode)
                self.assertIn("missing or modified", result.stderr)
                self.assertEqual(before, observations_path.read_bytes())

    def test_acquire_rejects_unsafe_malformed_or_duplicate_plan_before_token_check(self):
        def unsupported_kind(plan):
            plan["units"][0]["kind"] = "unsupported"

        def unsafe_unit_id(plan):
            plan["units"][0]["unit_id"] = "../escape"

        def missing_node_id(plan):
            plan["units"][0].pop("node_id")

        def duplicate_unit_id(plan):
            plan["units"].append(dict(plan["units"][0]))

        def malformed_selection(plan):
            plan["selection"] = []

        def malformed_comment_link(plan):
            plan["comment_links"][0]["unit_id"] = "render:missing"

        def mismatched_render_settings(plan):
            plan["units"][0]["scale"] = 2

        def noncanonical_unit(plan):
            plan["units"][0]["name"] = "Changed after planning"

        def noncanonical_comment_link(plan):
            link = plan["comment_links"][0]
            link["strategy"] = (
                "smallest-covering"
                if link["strategy"] != "smallest-covering"
                else "exact-node"
            )

        def missing_flows(plan):
            plan.pop("flows")

        def malformed_flow(plan):
            plan["flows"] = [{"flow_id": "broken"}]

        def invalid_flow_reference(plan):
            plan["flows"] = [{
                "flow_id": "prototype:0:1:missing",
                "name": "Broken",
                "kind": "prototype",
                "page_id": "0:1",
                "sections": [],
                "start_node_ids": ["missing"],
                "unresolved_start_node_ids": [],
                "ordering": "topological-layers",
                "steps": [{
                    "order": 0,
                    "nodes": [{
                        "node_id": "missing",
                        "name": "Missing",
                        "type": "FRAME",
                    }],
                }],
                "edges": [],
                "unresolved_edges": [],
                "branch_node_ids": [],
                "join_node_ids": [],
                "cyclic": False,
                "complete": True,
            }]

        def malformed_unresolved_connector(plan):
            plan["unresolved_connectors"] = [{"connector_node_id": "missing"}]

        cases = (
            ("unsupported kind", unsupported_kind, "unsupported kind"),
            ("unsafe unit id", unsafe_unit_id, "unit_id"),
            ("missing node id", missing_node_id, "node_id"),
            ("duplicate unit id", duplicate_unit_id, "duplicate"),
            ("malformed selection", malformed_selection, "selection"),
            ("malformed comment link", malformed_comment_link, "status or unit_id"),
            ("mismatched render settings", mismatched_render_settings, "render settings"),
            ("noncanonical unit", noncanonical_unit, "units do not match raw"),
            (
                "noncanonical comment link",
                noncanonical_comment_link,
                "comment links do not match raw",
            ),
            ("missing flows", missing_flows, "flows must be an array"),
            ("malformed flow", malformed_flow, "flow 0"),
            ("invalid flow reference", invalid_flow_reference, "node evidence"),
            (
                "malformed unresolved connector",
                malformed_unresolved_connector,
                "connector_node_id",
            ),
        )
        for name, mutate, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / "demo"
                self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
                plan_path = workspace / "render-plan.json"
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                mutate(plan)
                plan_path.write_text(json.dumps(plan), encoding="utf-8")

                result = self.run_tool("acquire", "--workspace", str(workspace))

                self.assertNotEqual(0, result.returncode)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("FIGMA_TOKEN", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_acquire_rejects_plan_after_normalized_comments_change(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            self.assertEqual(0, self.run_tool("demo", "--workspace", str(workspace)).returncode)
            normalized_path = workspace / "normalized.json"
            normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
            normalized["threads"] = []
            normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["normalized.json"]["sha256"] = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = self.run_tool("acquire", "--workspace", str(workspace))
            self.assertNotEqual(0, result.returncode)
            self.assertIn("does not match", result.stderr)
            self.assertNotIn("FIGMA_TOKEN", result.stderr)


if __name__ == "__main__":
    unittest.main()

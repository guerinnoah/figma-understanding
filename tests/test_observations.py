import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from figma_understanding.acquire import acquire_visuals
from figma_understanding.manifest import sha256_file
from figma_understanding.observations import record_observation, valid_observations
from figma_understanding.planner import build_render_plan
from figma_understanding.reporting import build_frontend_handoff
from tests.helpers import png


class Client:
    def render_urls(self, file_key, node_ids, *, version, batch_size=50):
        return {node_id: f"https://s3-alpha.figma.com/{node_id}.png" for node_id in node_ids}

    def original_urls(self, file_key):
        return {}


class ObservationTests(unittest.TestCase):
    def test_invalid_observation_file_does_not_create_inspection_state(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "children": [{"id": "1:1", "type": "FRAME", "name": "Screen"}],
            }],
        }
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

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            visual = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=Client(),
                downloader=downloader,
                plan=build_render_plan(document),
            )
            unit_id = visual["assets"][0]["unit_id"]
            visual_before = (workspace / "visual-manifest.json").read_bytes()
            (workspace / "observations.json").write_text("not json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                record_observation(workspace, unit_id, {"layout": ["Vertical"]})
            self.assertEqual(visual_before, (workspace / "visual-manifest.json").read_bytes())
            self.assertNotIn("inspected", visual["assets"][0]["status"])

    def test_observations_are_bound_to_downloaded_asset_hash(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "children": [{"id": "1:1", "type": "FRAME", "name": "Screen"}],
            }],
        }
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

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=Client(),
                downloader=downloader,
                plan=build_render_plan(document),
            )
            unit_id = manifest["assets"][0]["unit_id"]
            saved = record_observation(workspace, unit_id, {
                "layout": ["Centered card with vertical content"],
                "uncertainties": ["Responsive behavior is not shown"],
            })
            manifest = json.loads((workspace / "visual-manifest.json").read_text())
            valid, stale = valid_observations(workspace, manifest)
            self.assertEqual(unit_id, saved["unit_id"])
            self.assertEqual([unit_id], [item["unit_id"] for item in valid])
            self.assertEqual([], stale)
            self.assertNotIn("inspected", manifest["assets"][0]["status"])

            asset_path = workspace / manifest["assets"][0]["local_path"]
            asset_path.write_bytes(b"changed")
            valid, stale = valid_observations(workspace, manifest)
            self.assertEqual([], valid)
            self.assertEqual([unit_id], stale)

    def test_valid_observations_are_included_in_handoff(self):
        root = Path(__file__).resolve().parents[1]
        tool = root / "scripts" / "figma_tool.py"
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

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "demo"
            result = subprocess.run(
                [sys.executable, str(tool), "demo", "--workspace", str(workspace)],
                cwd=root,
                text=True,
                capture_output=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            raw = json.loads((workspace / "raw" / "file.json").read_text())
            reviewed = json.loads((workspace / "render-plan.json").read_text())
            inventory = json.loads((workspace / "manifest.json").read_text())
            visual = acquire_visuals(
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
            )
            unit_id = visual["assets"][0]["unit_id"]
            record_observation(workspace, unit_id, {"layout": ["Vertical checkout layout"]})
            handoff = build_frontend_handoff(workspace)
            self.assertEqual(1, handoff["coverage"]["observed_visual_units"])
            self.assertEqual(unit_id, handoff["observations"][0]["unit_id"])
            evidence = next(asset for asset in handoff["evidence_assets"] if asset["unit_id"] == unit_id)
            self.assertTrue(evidence["observed"])
            self.assertTrue(evidence["inspected"])

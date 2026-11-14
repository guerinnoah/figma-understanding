import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from figma_understanding.acquire import acquire_visuals, asset_is_valid
from figma_understanding.api import FigmaBatchError
from figma_understanding.planner import build_render_plan
from tests.helpers import png


class FakeClient:
    def render_urls(self, file_key, node_ids, *, version, batch_size=50):
        return {node_id: (None if node_id == "2:1" else f"https://images.figma.com/{node_id}.png") for node_id in node_ids}

    def original_urls(self, file_key):
        return {"img": "https://images.figma.com/original.png"}


class AcquireTests(unittest.TestCase):
    document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
        {"id": "1:1", "type": "FRAME", "name": "Frame"},
        {"id": "2:1", "type": "TEXT", "name": "Loose note"},
    ]}]}

    def plan(self, document=None, **kwargs):
        return build_render_plan(document or self.document, **kwargs)

    def test_statuses_stay_separate_and_partial_batch_survives(self):
        body = png(4, 5)
        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 4, "height": 5, "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=FakeClient(), downloader=downloader, plan=self.plan())
            self.assertEqual(2, manifest["summary"]["planned"])
            self.assertEqual(1, manifest["summary"]["downloaded"])
            self.assertNotIn("inspected", manifest["summary"])
            self.assertNotIn("plan", manifest)
            failed = next(asset for asset in manifest["assets"] if asset["node_id"] == "2:1")
            self.assertIn("no asset URL", failed["status"]["error"])
            good = next(asset for asset in manifest["assets"] if asset["status"]["downloaded"])
            self.assertNotIn("inspected", good["status"])
            self.assertNotIn("source_url", good)
            self.assertNotIn("final_url", good)

    def test_partial_batch_error_still_downloads_completed_urls(self):
        class PartialClient(FakeClient):
            def render_urls(self, file_key, node_ids, *, version, batch_size=50):
                raise FigmaBatchError("later batch failed", partial={"1:1": "https://images.figma.com/1.png"})
        body = png()
        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            manifest = acquire_visuals(file_key="key", design_version="v1", workspace=Path(directory), client=PartialClient(), downloader=downloader, plan=self.plan())
        good = next(asset for asset in manifest["assets"] if asset["node_id"] == "1:1")
        failed = next(asset for asset in manifest["assets"] if asset["node_id"] == "2:1")
        self.assertTrue(good["status"]["downloaded"])
        self.assertIn("later batch failed", failed["status"]["error"])

    def test_include_originals_downloads_unpinned_typed_asset(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "RECTANGLE", "name": "Image", "fills": [{"type": "IMAGE", "imageRef": "img"}]},
        ]}]}
        body = png()
        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=FakeClient(), include_originals=True, downloader=downloader, plan=self.plan(document, include_originals=True))
            original = next(asset for asset in manifest["assets"] if asset["kind"] == "original")
            self.assertFalse(original["historically_pinned"])
            self.assertTrue(original["status"]["downloaded"])
            self.assertTrue(original["local_path"].endswith(".png"))
            self.assertEqual("image/png", original["content_type"])
            self.assertNotIn("final_url", original)

    def test_resume_preserves_valid_download_and_skips_redownload(self):
        body = png()
        calls = []
        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = self.plan()
            first = acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=FakeClient(), downloader=downloader, plan=plan)
            good = next(asset for asset in first["assets"] if asset["status"]["downloaded"])
            second = acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=FakeClient(), downloader=downloader, plan=plan)
            resumed = next(asset for asset in second["assets"] if asset["unit_id"] == good["unit_id"])
            self.assertEqual(good["sha256"], resumed["sha256"])
            self.assertEqual(good["local_path"], resumed["local_path"])
            self.assertEqual(1, len(calls))

    def test_resume_rejects_manifest_with_non_object_source(self):
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
            plan = self.plan()
            acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                plan=plan,
            )
            manifest_path = workspace / "visual-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"] = None
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "different inventory"):
                acquire_visuals(
                    file_key="key",
                    design_version="v1",
                    workspace=workspace,
                    client=FakeClient(),
                    downloader=downloader,
                    plan=plan,
                )

    def test_resume_rebinds_unchanged_plan_to_regenerated_plan_hash(self):
        body = png()
        calls = []
        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        provenance = {
            "inventory_manifest_sha256": "inventory",
            "raw_file_sha256": "raw",
            "plan_sha256": "v3-plan",
        }
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = self.plan()
            first = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                inventory_provenance=provenance,
                plan=plan,
            )
            provenance["plan_sha256"] = "v4-plan"
            second = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                inventory_provenance=provenance,
                plan=plan,
            )

            self.assertEqual("v4-plan", second["source"]["plan_sha256"])
            self.assertEqual(first["summary"]["downloaded"], second["summary"]["downloaded"])
            self.assertEqual(1, len(calls))

    def test_replanned_manifest_preserves_compatible_downloads_and_refreshes_comment_links(self):
        body = png()
        calls = []
        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        provenance = {
            "inventory_manifest_sha256": "inventory",
            "raw_file_sha256": "raw",
            "plan_sha256": "before",
        }
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            initial_plan = self.plan()
            first = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                inventory_provenance=provenance,
                plan=initial_plan,
            )
            plan = build_render_plan(self.document, threads=[
                {"thread_id": "review", "node_id": "1:1", "node_reference_valid": True},
            ])
            provenance["plan_sha256"] = "after"
            second = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                inventory_provenance=provenance,
                plan=plan,
            )

            preserved = next(asset for asset in second["assets"] if asset["node_id"] == "1:1")
            self.assertTrue(preserved["status"]["downloaded"])
            self.assertEqual(["review"], preserved["thread_ids"])
            self.assertNotIn("plan", second)
            self.assertEqual(first["summary"]["downloaded"], second["summary"]["downloaded"])
            self.assertEqual(1, len(calls))

    def test_role_filter_acquires_overview_without_detail(self):
        body = png()
        calls = []
        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            manifest = acquire_visuals(file_key="key", design_version="v1", workspace=Path(directory), client=FakeClient(), downloader=downloader, plan=self.plan(), roles={"overview"})
        overview = next(asset for asset in manifest["assets"] if asset["node_id"] == "1:1")
        detail = next(asset for asset in manifest["assets"] if asset["node_id"] == "2:1")
        self.assertTrue(overview["status"]["downloaded"])
        self.assertFalse(detail["status"]["fetched"])
        self.assertIsNone(detail["status"]["error"])
        self.assertEqual(1, len(calls))

    def test_detail_filter_acquires_multi_role_unit(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Annotated", "annotations": [{"label": "Review"}]},
        ]}]}
        body = png()

        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}

        with tempfile.TemporaryDirectory() as directory:
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=Path(directory),
                client=FakeClient(),
                downloader=downloader,
                plan=self.plan(document),
                roles={"detail"},
            )
        self.assertTrue(manifest["assets"][0]["status"]["downloaded"])

    def test_resume_redownloads_modified_asset_and_restores_integrity(self):
        body = png()
        calls = []

        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            plan = self.plan()
            first = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                plan=plan,
            )
            asset = next(value for value in first["assets"] if value["status"]["downloaded"])
            (workspace / asset["local_path"]).write_bytes(b"modified")
            second = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                plan=plan,
            )
            repaired = next(value for value in second["assets"] if value["unit_id"] == asset["unit_id"])
            self.assertTrue(repaired["status"]["downloaded"])
            self.assertTrue(asset_is_valid(workspace, repaired))
            self.assertEqual(body, (workspace / repaired["local_path"]).read_bytes())
            self.assertEqual(2, len(calls))

    def test_failed_url_is_refetched_on_resume(self):
        class CountingClient(FakeClient):
            def __init__(self):
                self.calls = 0

            def render_urls(self, file_key, node_ids, *, version, batch_size=50):
                self.calls += 1
                return super().render_urls(file_key, node_ids, version=version, batch_size=batch_size)

        client = CountingClient()

        def failing_downloader(url, destination, **kwargs):
            raise OSError("expired")

        body = png()

        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}

        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Frame"},
        ]}]}
        plan = self.plan(document)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=client, downloader=failing_downloader, plan=plan)
            manifest = acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=client, downloader=downloader, plan=plan)
        self.assertEqual(2, client.calls)
        self.assertTrue(manifest["assets"][0]["status"]["downloaded"])

    def test_original_url_failure_does_not_poison_cached_original(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "RECTANGLE", "fills": [{"type": "IMAGE", "imageRef": "img1"}]},
            {"id": "1:2", "type": "RECTANGLE", "fills": [{"type": "IMAGE", "imageRef": "img2"}]},
        ]}]}

        class OriginalClient(FakeClient):
            def __init__(self, fail=False):
                self.fail = fail

            def original_urls(self, file_key):
                if self.fail:
                    raise OSError("unavailable")
                return {
                    "img1": "https://images.figma.com/img1.png",
                    "img2": "https://images.figma.com/img2.png",
                }

        body = png()

        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}

        plan = build_render_plan(document, include_originals=True)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=OriginalClient(),
                include_originals=True,
                downloader=downloader,
                plan=plan,
                unit_ids={"original:img1"},
            )
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=OriginalClient(fail=True),
                include_originals=True,
                downloader=downloader,
                plan=plan,
                unit_ids={"original:img2"},
            )
        assets = {asset["unit_id"]: asset for asset in manifest["assets"]}
        self.assertTrue(assets["original:img1"]["status"]["downloaded"])
        self.assertIsNone(assets["original:img1"]["status"]["error"])
        self.assertIn("unavailable", assets["original:img2"]["status"]["error"])

    def test_failed_original_post_processing_removes_download(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "RECTANGLE", "fills": [{"type": "IMAGE", "imageRef": "img"}]},
        ]}]}

        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"private bytes")
            return {"sha256": "hash", "bytes": 13, "width": 2, "height": 3, "content_type": "image/unknown"}

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                include_originals=True,
                downloader=downloader,
                plan=self.plan(document, include_originals=True),
                unit_ids={"original:img"},
            )
            self.assertEqual([], list((workspace / "visuals").iterdir()))
            self.assertFalse(manifest["assets"][-1]["status"]["downloaded"])

    def test_unit_filter_acquires_only_requested_unit(self):
        body = png()
        calls = []
        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        requested = "render:1-1"
        with tempfile.TemporaryDirectory() as directory:
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=Path(directory),
                client=FakeClient(),
                downloader=downloader,
                plan=self.plan(),
                unit_ids={requested},
            )
        selected = next(asset for asset in manifest["assets"] if asset["unit_id"] == requested)
        skipped = next(asset for asset in manifest["assets"] if asset["unit_id"] != requested)
        self.assertTrue(selected["status"]["downloaded"])
        self.assertFalse(skipped["status"]["fetched"])
        self.assertEqual(1, len(calls))

    def test_unit_filter_rejects_unknown_unit_before_writing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(ValueError, "unknown visual unit"):
                acquire_visuals(
                    file_key="key",
                    design_version="v1",
                    workspace=workspace,
                    client=FakeClient(),
                    plan=self.plan(),
                    unit_ids={"render:missing"},
                )
            self.assertFalse((workspace / "visual-manifest.json").exists())

    def test_comment_thread_ids_are_copied_to_visual_assets(self):
        body = png()
        plan = {
            "units": [{
                "unit_id": "render:1-1", "kind": "render", "roles": ["overview"],
                "node_id": "1:1", "page_id": "p", "name": "Frame", "format": "png", "scale": 1,
                "coverage_node_ids": ["1:1"], "thread_ids": ["comment-1"],
            }],
            "comment_links": [{"thread_id": "comment-1", "unit_id": "render:1-1", "status": "linked", "strategy": "exact-node"}],
        }
        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as directory:
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=Path(directory),
                client=FakeClient(),
                downloader=downloader,
                plan=plan,
            )
        self.assertEqual(["comment-1"], manifest["assets"][0]["thread_ids"])
        self.assertNotIn("plan", manifest)

    def test_resume_drops_assets_removed_from_reviewed_plan(self):
        body = png()
        calls = []

        def downloader(url, destination, **kwargs):
            calls.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}

        narrowed_document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "children": [{"id": "1:1", "type": "FRAME", "name": "Frame"}],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                plan=self.plan(),
            )
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                plan=self.plan(narrowed_document),
            )

            self.assertEqual(["render:1-1"], [asset["unit_id"] for asset in manifest["assets"]])
            self.assertEqual(1, len(calls))

    def test_asset_integrity_rejects_hash_valid_file_outside_visuals(self):
        body = png()

        def downloader(url, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
            return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "width": 2, "height": 3, "content_type": "image/png"}

        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Frame"},
        ]}]}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = acquire_visuals(
                file_key="key",
                design_version="v1",
                workspace=workspace,
                client=FakeClient(),
                downloader=downloader,
                plan=self.plan(document),
            )
            outside = workspace / "inventory.json"
            outside.write_bytes(body)
            manifest["assets"][0]["local_path"] = "inventory.json"
            manifest["assets"][0]["sha256"] = hashlib.sha256(body).hexdigest()
            self.assertFalse(asset_is_valid(workspace, manifest["assets"][0]))

    def test_interrupted_download_is_recorded(self):
        def downloader(url, destination, **kwargs):
            raise OSError("connection reset")
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [{"id": "1:1", "type": "FRAME", "name": "Frame"}]}]}
        with tempfile.TemporaryDirectory() as directory:
            manifest = acquire_visuals(file_key="key", design_version=None, workspace=Path(directory), client=FakeClient(), downloader=downloader, plan=self.plan(document))
            self.assertEqual(1, manifest["summary"]["failed"])
            self.assertFalse(manifest["assets"][0]["historically_pinned"])

    def test_keyboard_interrupt_is_persisted_with_planned_count(self):
        class InterruptedClient(FakeClient):
            def render_urls(self, file_key, node_ids, *, version, batch_size=50):
                raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaises(KeyboardInterrupt):
                acquire_visuals(file_key="key", design_version="v1", workspace=workspace, client=InterruptedClient(), plan=self.plan())
            saved = json.loads((workspace / "visual-manifest.json").read_text())
            self.assertEqual(2, saved["summary"]["planned"])
            self.assertTrue(all(asset["status"]["error"] == "acquisition interrupted" for asset in saved["assets"]))


if __name__ == "__main__":
    unittest.main()


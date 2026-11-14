import json
import tempfile
import unittest
from pathlib import Path

from figma_understanding.api import FigmaAPIError
from figma_understanding.inventory import ingest, normalize, validate_workspace
from figma_understanding.source import parse_figma_url


FILE = {
    "name": "Pilot",
    "version": "v42",
    "lastModified": "2026-11-13T10:00:00Z",
    "editorType": "figma",
    "styles": {"style:heading": {"name": "Heading", "styleType": "TEXT"}},
    "components": {},
    "componentSets": {},
    "document": {"id": "0:0", "type": "DOCUMENT", "name": "Document", "children": [{
        "id": "0:1", "type": "CANVAS", "name": "Page", "children": [{
            "id": "1:1", "type": "FRAME", "name": "Screen", "annotations": [{"label": "Check"}],
            "layoutMode": "VERTICAL", "itemSpacing": 16, "paddingTop": 24,
            "children": [
                {
                    "id": "1:2", "type": "TEXT", "name": "Title", "characters": "Hello",
                    "style": {"fontFamily": "Inter", "fontWeight": 700, "fontSize": 32},
                    "styles": {"text": "style:heading"},
                },
                {"id": "1:3", "type": "RECTANGLE", "name": "Photo", "fills": [{"type": "IMAGE", "imageRef": "img-1"}]},
            ],
        }],
    }]},
}
COMMENTS = {"comments": [
    {"id": "c1", "message": "Root", "created_at": "2026-11-13T10:01:00Z", "resolved_at": "2026-11-13T10:03:00Z", "client_meta": {"node_id": "1:1"}},
    {"id": "c2", "parent_id": "c1", "message": "Reply", "created_at": "2026-11-13T10:02:00Z"},
    {"id": "c3", "parent_id": "missing", "message": "Orphan", "created_at": "2026-11-13T10:04:00Z"},
]}


class Client:
    def __init__(self, *, file_error=None, comments_error=None):
        self.file_error = file_error
        self.comments_error = comments_error

    def file(self, file_key):
        if self.file_error:
            raise self.file_error
        return FILE

    def comments(self, file_key):
        if self.comments_error:
            raise self.comments_error
        return COMMENTS


class InventoryTests(unittest.TestCase):
    source = parse_figma_url("https://www.figma.com/design/PilotKey123/Pilot?node-id=1-1")

    def test_normalizes_nodes_threads_and_design_evidence(self):
        value = normalize(FILE, COMMENTS)
        self.assertNotIn("relationships", value)
        self.assertEqual(5, len(value["nodes"]))
        title = next(node for node in value["nodes"] if node["id"] == "1:2")
        self.assertEqual("Hello", title["characters"])
        self.assertEqual("1:1", title["parent_id"])
        annotated = next(node for node in value["nodes"] if node["id"] == "1:1")
        image = next(node for node in value["nodes"] if node["id"] == "1:3")
        self.assertEqual([{"label": "Check"}], annotated["annotations"])
        self.assertEqual(["img-1"], image["image_refs"])
        self.assertEqual("VERTICAL", annotated["implementation"]["layout"]["mode"])
        self.assertEqual(16, annotated["implementation"]["layout"]["item_spacing"])
        self.assertEqual("Inter", title["implementation"]["typography"]["font_family"])
        self.assertEqual("style:heading", title["implementation"]["bindings"]["styles"]["text"])
        self.assertEqual("Heading", value["design_system"]["styles"]["style:heading"]["name"])
        root = next(thread for thread in value["threads"] if thread["thread_id"] == "c1")
        self.assertTrue(root["resolved"])
        self.assertTrue(root["node_reference_valid"])
        self.assertEqual(["c2"], [reply["id"] for reply in root["replies"]])
        self.assertTrue(any(thread["thread_id"] == "orphan:missing" for thread in value["threads"]))

    def test_visibility_is_inherited_and_reactions_are_preserved(self):
        file_response = {
            **FILE,
            "document": {
                "id": "0:0",
                "type": "DOCUMENT",
                "children": [{
                    "id": "0:1",
                    "type": "CANVAS",
                    "children": [{
                        "id": "1:1",
                        "type": "FRAME",
                        "visible": False,
                        "children": [{
                            "id": "1:2",
                            "type": "TEXT",
                            "characters": "Deprecated copy",
                            "reactions": [{"action": {"destinationId": "2:1"}}],
                        }],
                    }],
                }],
            },
        }
        value = normalize(file_response, None)
        child = next(node for node in value["nodes"] if node["id"] == "1:2")
        self.assertFalse(child["visible"])
        self.assertEqual(
            "2:1",
            child["implementation"]["prototype"]["reactions"][0]["action"]["destinationId"],
        )

    def test_figjam_connector_endpoints_and_caps_are_preserved(self):
        file_response = {
            **FILE,
            "document": {
                "id": "0:0",
                "type": "DOCUMENT",
                "children": [{
                    "id": "0:1",
                    "type": "CANVAS",
                    "children": [{
                        "id": "1:1",
                        "type": "CONNECTOR",
                        "connectorStart": {"endpointNodeId": "2:1"},
                        "connectorEnd": {"endpointNodeId": "3:1"},
                        "connectorEndStrokeCap": "LINE_ARROW",
                        "connectorLineType": "ELBOWED",
                    }],
                }],
            },
        }

        value = normalize(file_response, None)
        connector = next(node for node in value["nodes"] if node["id"] == "1:1")

        self.assertEqual("2:1", connector["implementation"]["connector"]["start"]["endpointNodeId"])
        self.assertEqual("3:1", connector["implementation"]["connector"]["end"]["endpointNodeId"])
        self.assertEqual("LINE_ARROW", connector["implementation"]["connector"]["end_cap"])

    def test_ingest_preserves_raw_data_hashes_and_request_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "snapshot"
            manifest = ingest(source=self.source, workspace=workspace, client=Client())
            self.assertEqual("complete", manifest["overall_status"])
            self.assertFalse(manifest["requests_are_atomic"])
            self.assertEqual("complete", manifest["requests"]["comments"]["status"])
            self.assertEqual("v42", manifest["design_version"])
            self.assertEqual([], validate_workspace(workspace))
            self.assertNotIn("inventory.md", manifest["artifacts"])
            self.assertFalse((workspace / "inventory.md").exists())
            raw = json.loads((workspace / "raw" / "file.json").read_text())
            self.assertEqual(FILE, raw)

    def test_comment_failure_is_partial_not_file_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "snapshot"
            manifest = ingest(source=self.source, workspace=workspace, client=Client(comments_error=FigmaAPIError("missing comment scope", status=403)))
            self.assertEqual("partial", manifest["overall_status"])
            self.assertEqual({"file": "complete", "comments": "failed"}, manifest["extraction_status"])
            self.assertFalse((workspace / "raw" / "comments.json").exists())
            self.assertEqual(0, manifest["counts"]["threads"])

    def test_file_failure_skips_comments_and_does_not_claim_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "snapshot"
            manifest = ingest(source=self.source, workspace=workspace, client=Client(file_error=FigmaAPIError("not found", status=404)))
            self.assertEqual("incomplete", manifest["overall_status"])
            self.assertEqual({"file": "failed", "comments": "skipped"}, manifest["extraction_status"])
            self.assertEqual({}, manifest["artifacts"])
            self.assertFalse((workspace / "raw" / "file.json").exists())

    def test_snapshots_are_immutable_and_hash_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "snapshot"
            ingest(source=self.source, workspace=workspace, client=Client())
            with self.assertRaises(FileExistsError):
                ingest(source=self.source, workspace=workspace, client=Client())
            (workspace / "normalized.json").write_text("{}", encoding="utf-8")
            self.assertIn("artifact hash mismatch: normalized.json", validate_workspace(workspace))

    def test_validation_rejects_removed_artifact_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "snapshot"
            ingest(source=self.source, workspace=workspace, client=Client())
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["artifacts"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            errors = validate_workspace(workspace)
            self.assertTrue(any("required artifact is unlisted" in error for error in errors))

    def test_normalization_failure_is_recorded(self):
        class BadClient(Client):
            def file(self, file_key):
                return {"name": "Broken", "version": "v1"}
        with tempfile.TemporaryDirectory() as directory:
            manifest = ingest(source=self.source, workspace=Path(directory) / "snapshot", client=BadClient())
            self.assertEqual("failed", manifest["processing_status"])
            self.assertEqual("incomplete", manifest["overall_status"])
            self.assertEqual("normalize", manifest["errors"][-1]["stage"])


if __name__ == "__main__":
    unittest.main()


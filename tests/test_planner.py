import unittest

from figma_understanding.planner import build_render_plan


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.document = {"type": "DOCUMENT", "children": [{"id": "p:1", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Screen", "children": [{"id": "1:2", "type": "TEXT", "name": "Label"}, {"id": "1:4", "type": "TEXT", "name": "Hidden label", "visible": False}]},
            {"id": "2:1", "type": "TEXT", "name": "Loose note"},
            {"id": "3:1", "type": "RECTANGLE", "name": "Screenshot", "fills": [{"type": "IMAGE", "imageRef": "ref-a"}]},
            {"id": "4:1", "type": "FRAME", "name": "Hidden", "visible": False},
            {"id": "5:1", "type": "STICKY", "name": "Idea"},
            {"id": "6:1", "type": "RECTANGLE", "name": "Annotated", "annotations": [{"label": "Check"}]},
            {"id": "7:1", "type": "FRAME", "name": "Hidden parent", "visible": False, "children": [{"id": "7:2", "type": "TEXT", "name": "Loose note"}]},
            {"id": "8:1", "type": "RECTANGLE", "name": "Reused", "fills": [{"type": "IMAGE", "imageRef": "ref-a"}]},
            {"id": "9:1", "type": "RECTANGLE", "name": "Hidden fill", "fills": [{"type": "IMAGE", "imageRef": "hidden-ref", "visible": False}]},
        ]}]}

    def test_overviews_and_loose_details_are_distinct(self):
        plan = build_render_plan(self.document)
        roles_by_node = {unit["node_id"]: unit["roles"] for unit in plan["units"]}
        self.assertEqual(["overview"], roles_by_node["1:1"])
        self.assertEqual(["detail"], roles_by_node["2:1"])
        self.assertEqual(["detail"], roles_by_node["3:1"])
        self.assertNotIn("4:1", roles_by_node)
        self.assertEqual(["detail"], roles_by_node["5:1"])
        self.assertEqual(["detail"], roles_by_node["6:1"])
        self.assertFalse(any(unit["node_id"] == "7:2" for unit in plan["units"]))
        self.assertTrue(all("role" not in unit for unit in plan["units"]))
        self.assertNotIn("coverage", plan)

    def test_originals_are_opt_in_and_unpinned(self):
        without = build_render_plan(self.document)
        with_originals = build_render_plan(self.document, include_originals=True)
        self.assertFalse(any(unit["kind"] == "original" for unit in without["units"]))
        original = next(unit for unit in with_originals["units"] if unit["kind"] == "original")
        self.assertFalse(original["historically_pinned"])
        self.assertEqual(["3:1", "8:1"], original["source_node_ids"])
        self.assertFalse(any(unit.get("image_ref") == "hidden-ref" for unit in with_originals["units"]))

    def test_one_render_can_hold_overview_and_detail_roles(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Annotated frame", "annotations": [{"label": "a"}]},
        ]}]}
        plan = build_render_plan(document)
        self.assertEqual(1, len(plan["units"]))
        self.assertEqual(["overview", "detail"], plan["units"][0]["roles"])

    def test_selection_targets_nodes_pages_and_detail_limit(self):
        targeted = build_render_plan(self.document, node_ids={"1:2"})
        self.assertEqual({"render:1-1", "render:1-2"}, {
            unit["unit_id"] for unit in targeted["units"]
        })
        self.assertEqual(["1:2"], targeted["selection"]["node_ids"])

        limited = build_render_plan(self.document, max_detail=2)
        detail_only = [
            unit
            for unit in limited["units"]
            if "overview" not in unit["roles"]
        ]
        self.assertEqual(2, len(detail_only))

        with self.assertRaisesRegex(ValueError, "unknown page"):
            build_render_plan(self.document, page_ids={"missing"})
        with self.assertRaisesRegex(ValueError, "unknown node"):
            build_render_plan(self.document, node_ids={"missing"})

    def test_named_flow_follows_prototype_destinations(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Checkout", "nodeId": "1:1"}],
                "children": [
                    {
                        "id": "1:1",
                        "type": "FRAME",
                        "name": "Cart",
                        "children": [{
                            "id": "1:2",
                            "type": "INSTANCE",
                            "interactions": [{"actions": [{"destinationId": "2:1"}]}],
                        }],
                    },
                    {"id": "2:1", "type": "FRAME", "name": "Payment"},
                    {"id": "3:1", "type": "FRAME", "name": "Unrelated"},
                ],
            }],
        }
        plan = build_render_plan(document, flow_names={"Checkout"})
        self.assertEqual({"render:1-1", "render:2-1"}, {
            unit["unit_id"] for unit in plan["units"]
        })
        self.assertEqual(["1:1", "2:1"], plan["selection"]["resolved_flow_node_ids"])
        with self.assertRaisesRegex(ValueError, "unknown flow"):
            build_render_plan(document, flow_names={"Missing"})

        document["children"][0]["flowStartingPoints"].append({
            "name": "checkout",
            "nodeId": "3:1",
        })
        with self.assertRaisesRegex(ValueError, "ambiguous flow"):
            build_render_plan(document, flow_names={"Checkout"})

    def test_oversized_overview_adds_direct_child_tiles(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "children": [{
                    "id": "1:1",
                    "type": "SECTION",
                    "name": "Large board",
                    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 9000, "height": 3000},
                    "children": [{
                        "id": "1:2",
                        "type": "FRAME",
                        "name": "Tile",
                        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 1000, "height": 800},
                    }],
                }],
            }],
        }
        tile = next(
            unit
            for unit in build_render_plan(document)["units"]
            if unit["unit_id"] == "render:1-2"
        )
        self.assertEqual("1:1", tile["tile_parent_id"])
        self.assertEqual(["overview", "detail"], tile["roles"])
        self.assertIn("adaptive-tile", tile["review_flags"])

    def test_single_text_section_and_top_level_instance_are_overviews(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "name": "Board", "children": [
            {"id": "1:1", "type": "SECTION", "name": "Branch", "children": [
                {"id": "1:2", "type": "SHAPE_WITH_TEXT", "name": "Check flow below", "characters": "Check flow below"},
            ]},
            {"id": "2:1", "type": "INSTANCE", "name": "Checkout screen"},
        ]}]}
        plan = build_render_plan(document)
        overview_ids = {
            unit["node_id"]
            for unit in plan["units"]
            if "overview" in unit["roles"]
        }
        self.assertTrue({"1:1", "2:1"}.issubset(overview_ids))

    def test_original_selection_respects_page_and_prefers_canvas_render(self):
        document = {"type": "DOCUMENT", "children": [
            {"id": "p:1", "type": "CANVAS", "children": [
                {"id": "1:1", "type": "RECTANGLE", "name": "First", "fills": [{"type": "IMAGE", "imageRef": "shared"}]},
            ]},
            {"id": "p:2", "type": "CANVAS", "children": [
                {"id": "2:1", "type": "RECTANGLE", "name": "Second", "fills": [{"type": "IMAGE", "imageRef": "shared"}]},
            ]},
        ]}
        selected = build_render_plan(document, include_originals=True, page_ids={"p:2"})
        original = next(unit for unit in selected["units"] if unit["kind"] == "original")
        self.assertEqual("p:2", original["page_id"])
        self.assertEqual(["2:1"], original["source_node_ids"])
        self.assertTrue(selected["selection"]["include_originals"])

        limited = build_render_plan(document, include_originals=True, max_detail=1)
        detail = next(unit for unit in limited["units"] if unit["roles"] == ["detail"])
        self.assertEqual("render", detail["kind"])

    def test_native_figjam_media_types_are_detail_units(self):
        document = {"type": "DOCUMENT", "children": [{
            "id": "p",
            "type": "CANVAS",
            "children": [
                {"id": f"1:{index}", "type": node_type, "name": node_type}
                for index, node_type in enumerate(("MEDIA", "STAMP", "WASHI_TAPE", "HIGHLIGHT"), 1)
            ],
        }]}
        plan = build_render_plan(document)
        self.assertEqual(
            {"MEDIA", "STAMP", "WASHI_TAPE", "HIGHLIGHT"},
            {unit["name"] for unit in plan["units"] if "detail" in unit["roles"]},
        )

    def test_detail_units_include_ancestor_context(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "name": "Board", "children": [
            {"id": "1:1", "type": "SECTION", "name": "Import flow", "children": [
                {"id": "1:2", "type": "RECTANGLE", "name": "Screenshot", "fills": [{"type": "IMAGE", "imageRef": "ref"}]},
            ]},
        ]}]}
        detail = next(unit for unit in build_render_plan(document)["units"] if unit["node_id"] == "1:2")
        self.assertEqual(["Board", "Import flow"], [item["name"] for item in detail["context_path"]])

    def test_sparse_overview_is_flagged(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "SECTION", "name": "Sparse", "absoluteBoundingBox": {"x": 0, "y": 0, "width": 1000, "height": 1000}, "children": [
                {"id": "1:2", "type": "FRAME", "name": "A", "absoluteBoundingBox": {"x": 0, "y": 0, "width": 100, "height": 100}},
                {"id": "1:3", "type": "FRAME", "name": "B", "absoluteBoundingBox": {"x": 120, "y": 0, "width": 100, "height": 100}},
            ]},
        ]}]}
        overview = next(unit for unit in build_render_plan(document)["units"] if unit["node_id"] == "1:1")
        self.assertEqual(["sparse-content"], overview["review_flags"])
        self.assertEqual(0.022, overview["content_occupancy"])

    def test_comments_link_to_exact_then_smallest_covering_render(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "name": "Page", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Screen", "children": [
                {"id": "1:2", "type": "TEXT", "name": "Label"},
                {"id": "1:3", "type": "RECTANGLE", "name": "Screenshot", "fills": [{"type": "IMAGE", "imageRef": "ref"}]},
            ]},
        ]}]}
        threads = [
            {"thread_id": "exact", "node_id": "1:3", "node_reference_valid": True},
            {"thread_id": "covering", "node_id": "1:2", "node_reference_valid": True},
            {"thread_id": "stale", "node_id": "9:9", "node_reference_valid": False},
            {"thread_id": "unanchored", "node_id": None, "node_reference_valid": None},
        ]
        plan = build_render_plan(document, threads=threads)
        links = {link["thread_id"]: link for link in plan["comment_links"]}
        self.assertEqual(("render:1-3", "exact-node"), (links["exact"]["unit_id"], links["exact"]["strategy"]))
        self.assertEqual(("render:1-1", "smallest-covering"), (links["covering"]["unit_id"], links["covering"]["strategy"]))
        self.assertEqual((None, "invalid-node", "unlinked"), (links["stale"]["unit_id"], links["stale"]["strategy"], links["stale"]["status"]))
        self.assertEqual((None, "no-node"), (links["unanchored"]["unit_id"], links["unanchored"]["strategy"]))
        self.assertEqual(["render:1-1", "render:1-3"], links["exact"]["candidate_unit_ids"])
        self.assertEqual(["render:1-1"], links["covering"]["candidate_unit_ids"])
        self.assertEqual([], links["stale"]["candidate_unit_ids"])
        self.assertEqual([], links["unanchored"]["candidate_unit_ids"])
        exact = next(unit for unit in plan["units"] if unit["unit_id"] == "render:1-3")
        covering = next(unit for unit in plan["units"] if unit["unit_id"] == "render:1-1")
        self.assertEqual(["exact"], exact["thread_ids"])
        self.assertEqual(["covering"], covering["thread_ids"])

    def test_comment_prefers_smallest_of_multiple_covering_renders(self):
        document = {"type": "DOCUMENT", "children": [{"id": "p", "type": "CANVAS", "children": [
            {"id": "1:1", "type": "FRAME", "name": "Outer", "children": [
                {"id": "1:2", "type": "FRAME", "name": "Annotated inner", "annotations": [{"label": "Review"}], "children": [
                    {"id": "1:3", "type": "TEXT", "name": "Comment target"},
                ]},
                {"id": "1:4", "type": "TEXT", "name": "Outer sibling"},
            ]},
        ]}]}
        plan = build_render_plan(document, threads=[
            {"thread_id": "nested", "node_id": "1:3", "node_reference_valid": True},
        ])

        link = plan["comment_links"][0]
        self.assertEqual("render:1-2", link["unit_id"])
        self.assertEqual("smallest-covering", link["strategy"])
        self.assertEqual(["render:1-1", "render:1-2"], link["candidate_unit_ids"])


if __name__ == "__main__":
    unittest.main()


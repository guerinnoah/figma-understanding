import copy
import unittest

from figma_understanding.flows import flow_record_errors, unresolved_connector_errors
from figma_understanding.inventory import normalize_document_nodes
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
        flow = plan["flows"][0]
        self.assertEqual("prototype", flow["kind"])
        self.assertEqual("prototype-transition", flow["semantics"])
        self.assertEqual(
            [["1:1"], ["2:1"]],
            [[node["node_id"] for node in step["nodes"]] for step in flow["steps"]],
        )
        self.assertEqual("1:2", flow["edges"][0]["evidence_node_id"])
        self.assertEqual(["1:1", "1:2", "2:1"], flow["scope_node_ids"])
        with self.assertRaisesRegex(ValueError, "unknown flow"):
            build_render_plan(document, flow_names={"Missing"})

        document["children"][0]["flowStartingPoints"].append({
            "name": "checkout",
            "nodeId": "3:1",
        })
        with self.assertRaisesRegex(ValueError, "ambiguous flow"):
            build_render_plan(document, flow_names={"Checkout"})

    def test_legacy_prototype_fields_are_fallbacks(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "prototypeStartNodeID": "A",
                "flowStartingPoints": [],
                "children": [
                    {
                        "id": "A",
                        "type": "FRAME",
                        "name": "Start",
                        "transitionNodeID": "B",
                        "interactions": [{
                            "trigger": {"type": "ON_CLICK"},
                            "actions": [{
                                "type": "NODE",
                                "navigation": "NAVIGATE",
                                "destinationId": "B",
                            }],
                        }],
                    },
                    {"id": "B", "type": "FRAME", "name": "End"},
                ],
            }],
        }

        plan = build_render_plan(document)

        self.assertEqual(1, len(plan["flows"]))
        flow = plan["flows"][0]
        self.assertEqual(["A"], flow["start_node_ids"])
        self.assertEqual(1, len(flow["edges"]))
        self.assertEqual("NAVIGATE", flow["edges"][0]["action"])
        self.assertEqual("ON_CLICK", flow["edges"][0]["trigger"])

    def test_flow_layers_preserve_dependencies_and_incomplete_links(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Account", "nodeId": "A"}],
                "children": [
                    {
                        "id": "A",
                        "type": "FRAME",
                        "name": "Start",
                        "interactions": [
                            {
                                "trigger": {"type": "ON_CLICK"},
                                "actions": [
                                    {"type": "NAVIGATE", "destinationId": "B"},
                                    {"type": "NAVIGATE", "destinationId": "C"},
                                    {"type": "NAVIGATE", "destinationId": "MISSING"},
                                ],
                            },
                            {
                                "trigger": {"type": "AFTER_TIMEOUT"},
                                "actions": [{"type": "NAVIGATE", "destinationId": "B"}],
                            },
                        ],
                    },
                    {
                        "id": "B",
                        "type": "FRAME",
                        "name": "Review",
                        "interactions": [{"actions": [{"destinationId": "C"}]}],
                    },
                    {"id": "C", "type": "FRAME", "name": "Complete"},
                ],
            }],
        }

        flow = build_render_plan(document)["flows"][0]

        self.assertEqual("topological-layers", flow["ordering"])
        self.assertEqual(
            [["A"], ["B"], ["C"]],
            [[node["node_id"] for node in step["nodes"]] for step in flow["steps"]],
        )
        self.assertEqual(
            {"ON_CLICK", "AFTER_TIMEOUT"},
            {
                edge["trigger"]
                for edge in flow["edges"]
                if edge["destination_node_id"] == "B"
            },
        )
        self.assertFalse(flow["complete"])
        self.assertEqual("destination-node-missing", flow["unresolved_edges"][0]["reason"])
        self.assertFalse(any(
            edge["destination_node_id"] == "MISSING"
            for edge in flow["edges"]
        ))

        missing_start = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Missing", "nodeId": "GONE"}],
                "children": [],
            }],
        }
        broken = build_render_plan(missing_start)["flows"][0]
        self.assertEqual([], broken["steps"])
        self.assertEqual(["GONE"], broken["unresolved_start_node_ids"])
        self.assertEqual("unresolved", broken["ordering"])
        self.assertFalse(broken["complete"])

    def test_prototype_edges_keep_topology_without_treating_media_as_navigation(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Checkout", "nodeId": "A"}],
                "children": [
                    {
                        "id": "A",
                        "type": "FRAME",
                        "name": "Start",
                        "interactions": [{
                            "trigger": {"type": "ON_CLICK"},
                            "actions": [
                                {
                                    "type": "NODE",
                                    "navigation": "OVERLAY",
                                    "destinationId": "B",
                                },
                                {
                                    "type": "UPDATE_MEDIA_RUNTIME",
                                    "destinationId": "video",
                                    "mediaAction": "PLAY",
                                },
                                {
                                    "type": "CONDITIONAL",
                                    "conditionalBlocks": [{
                                        "condition": {},
                                        "actions": [{
                                            "type": "NODE",
                                            "navigation": "NAVIGATE",
                                            "destinationId": "C",
                                        }],
                                    }],
                                },
                            ],
                        }],
                    },
                    {"id": "B", "type": "FRAME", "name": "Overlay"},
                    {"id": "C", "type": "FRAME", "name": "Complete"},
                    {"id": "video", "type": "MEDIA", "name": "Demo"},
                ],
            }],
        }

        plan = build_render_plan(document)
        flow = plan["flows"][0]
        edges = {edge["destination_node_id"]: edge for edge in flow["edges"]}

        self.assertEqual({"B", "C"}, set(edges))
        self.assertEqual("OVERLAY", edges["B"]["action"])
        self.assertEqual("NAVIGATE", edges["C"]["action"])
        self.assertTrue(edges["C"]["conditional"])
        normalized = {node["id"]: node for node in normalize_document_nodes(document)}
        self.assertEqual([], flow_record_errors(plan["flows"], normalized, {"p"}))

    def test_hidden_sections_and_nodes_do_not_create_flow_evidence(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Visible", "nodeId": "A"}],
                "children": [
                    {
                        "id": "A",
                        "type": "FRAME",
                        "name": "Start",
                        "children": [{
                            "id": "hidden-action",
                            "type": "INSTANCE",
                            "visible": False,
                            "interactions": [{"actions": [{
                                "type": "NODE",
                                "navigation": "NAVIGATE",
                                "destinationId": "B",
                            }]}],
                        }],
                    },
                    {"id": "B", "type": "FRAME", "name": "Hidden destination", "visible": False},
                    {
                        "id": "s",
                        "type": "SECTION",
                        "name": "Collapsed",
                        "sectionContentsHidden": True,
                        "children": [
                            {"id": "X", "type": "SHAPE_WITH_TEXT", "name": "X"},
                            {"id": "Y", "type": "SHAPE_WITH_TEXT", "name": "Y"},
                            {
                                "id": "connector",
                                "type": "CONNECTOR",
                                "connectorStart": {"endpointNodeId": "X"},
                                "connectorEnd": {"endpointNodeId": "Y"},
                                "connectorEndStrokeCap": "LINE_ARROW",
                            },
                        ],
                    },
                ],
            }],
        }

        plan = build_render_plan(document)
        normalized = {node["id"]: node for node in normalize_document_nodes(document)}

        self.assertEqual([], plan["flows"][0]["edges"])
        self.assertFalse(any(flow["kind"] == "connector" for flow in plan["flows"]))
        self.assertFalse(normalized["X"]["visible"])
        self.assertNotIn("X", {unit["node_id"] for unit in plan["units"]})

    def test_flow_validators_reject_unhashable_ids_without_crashing(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Flow", "nodeId": "A"}],
                "children": [
                    {
                        "id": "A",
                        "type": "FRAME",
                        "interactions": [{"actions": [{"destinationId": "B"}]}],
                    },
                    {"id": "B", "type": "FRAME"},
                    {"id": "s", "type": "SECTION"},
                    {"id": "c", "type": "CONNECTOR"},
                ],
            }],
        }
        plan = build_render_plan(document)
        nodes = {node["id"]: node for node in normalize_document_nodes(document)}

        malformed_section = copy.deepcopy(plan["flows"][0])
        malformed_section["sections"] = [{"node_id": [], "name": "Bad"}]
        malformed_edge = copy.deepcopy(plan["flows"][0])
        malformed_edge["edges"][0]["source_node_id"] = []

        self.assertTrue(flow_record_errors([malformed_section], nodes, {"p"}))
        self.assertTrue(flow_record_errors([malformed_edge], nodes, {"p"}))
        self.assertTrue(unresolved_connector_errors([{
            "connector_node_id": "c",
            "page_id": [],
            "reason": [],
            "start_node_id": None,
            "end_node_id": None,
            "section_id": [],
            "start_cap": "NONE",
            "end_cap": "NONE",
        }], nodes, {"p"}))

    def test_flow_start_node_selector_must_be_unique(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Prototype", "nodeId": "A"}],
                "children": [
                    {
                        "id": "A",
                        "type": "FRAME",
                        "name": "Start",
                        "interactions": [{"actions": [{"destinationId": "B"}]}],
                    },
                    {"id": "B", "type": "FRAME", "name": "End"},
                    {
                        "id": "connector",
                        "type": "CONNECTOR",
                        "connectorStart": {"endpointNodeId": "A"},
                        "connectorEnd": {"endpointNodeId": "B"},
                        "connectorEndStrokeCap": "LINE_ARROW",
                    },
                ],
            }],
        }

        with self.assertRaisesRegex(ValueError, "ambiguous flow"):
            build_render_plan(document, flow_names={"A"})
        selected = build_render_plan(document, flow_names={"prototype:p:A"})
        self.assertEqual(["prototype:p:A"], [flow["flow_id"] for flow in selected["flows"]])

    def test_directional_figjam_connectors_produce_ordered_flow(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "children": [{
                    "id": "s:1",
                    "type": "SECTION",
                    "name": "User flows",
                    "children": [
                    {"id": "1:1", "type": "SHAPE_WITH_TEXT", "name": "Start"},
                    {"id": "2:1", "type": "SHAPE_WITH_TEXT", "name": "Review"},
                    {"id": "3:1", "type": "SHAPE_WITH_TEXT", "name": "Done"},
                    {
                        "id": "4:1",
                        "type": "CONNECTOR",
                        "connectorStart": {"endpointNodeId": "1:1"},
                        "connectorEnd": {"endpointNodeId": "2:1"},
                        "connectorEndStrokeCap": "LINE_ARROW",
                    },
                    {
                        "id": "4:2",
                        "type": "CONNECTOR",
                        "connectorStart": {"endpointNodeId": "3:1"},
                        "connectorEnd": {"endpointNodeId": "2:1"},
                        "connectorStartStrokeCap": "TRIANGLE_ARROW",
                    },
                    {
                        "id": "4:3",
                        "type": "CONNECTOR",
                        "connectorStart": {"endpointNodeId": "1:1"},
                        "connectorEnd": {"endpointNodeId": "3:1"},
                    },
                    {"id": "4:4", "type": "CONNECTOR"},
                    {
                        "id": "4:5",
                        "type": "CONNECTOR",
                        "connectorStart": {"endpointNodeId": "1:1"},
                        "connectorEnd": {"endpointNodeId": "3:1"},
                        "connectorStartStrokeCap": "LINE_ARROW",
                        "connectorEndStrokeCap": "TRIANGLE_ARROW",
                    },
                    {
                        "id": "5:1",
                        "type": "RECTANGLE",
                        "name": "Screenshot",
                        "fills": [{"type": "IMAGE", "imageRef": "screen"}],
                    },
                    ],
                }],
            }],
        }

        plan = build_render_plan(document)
        flow = plan["flows"][0]

        self.assertEqual("connector", flow["kind"])
        self.assertEqual("diagram-order", flow["semantics"])
        self.assertEqual([{"node_id": "s:1", "name": "User flows"}], flow["sections"])
        self.assertTrue(all(
            node["section_id"] == "s:1"
            for step in flow["steps"]
            for node in step["nodes"]
        ))
        self.assertEqual(
            [["1:1"], ["2:1"], ["3:1"]],
            [[node["node_id"] for node in step["nodes"]] for step in flow["steps"]],
        )
        self.assertEqual(
            [("1:1", "2:1"), ("2:1", "3:1")],
            [(edge["source_node_id"], edge["destination_node_id"]) for edge in flow["edges"]],
        )
        self.assertTrue(all("line_type" not in edge for edge in flow["edges"]))
        self.assertEqual(
            {"s:1", "1:1", "2:1", "3:1", "4:1", "4:2"},
            set(flow["scope_node_ids"]),
        )
        normalized_nodes = normalize_document_nodes(document)
        normalized_by_id = {node["id"]: node for node in normalized_nodes}
        self.assertEqual([], flow_record_errors(plan["flows"], normalized_by_id, {"p"}))
        self.assertEqual("no-directional-arrowhead", plan["unresolved_connectors"][0]["reason"])
        bidirectional = next(
            connector
            for connector in plan["unresolved_connectors"]
            if connector["connector_node_id"] == "4:5"
        )
        self.assertEqual("bidirectional-arrowheads", bidirectional["reason"])
        self.assertNotIn("4:5", {edge["evidence_node_id"] for edge in flow["edges"]})

        selected_connector = build_render_plan(document, node_ids={"4:1"})
        self.assertEqual(
            ["connector:4:1"],
            [selected_flow["flow_id"] for selected_flow in selected_connector["flows"]],
        )

        selected = build_render_plan(document, section_names={"User flows"})
        self.assertEqual(["s:1"], selected["selection"]["resolved_section_node_ids"])
        self.assertIn("render:5-1", {unit["unit_id"] for unit in selected["units"]})
        self.assertEqual(["connector:4:1"], [flow["flow_id"] for flow in selected["flows"]])
        self.assertEqual(
            {"4:3", "4:4", "4:5"},
            {connector["connector_node_id"] for connector in selected["unresolved_connectors"]},
        )
        selected_flow = build_render_plan(document, flow_names={"connector:4:1"})
        self.assertEqual(
            {"render:s-1", "render:1-1", "render:2-1", "render:3-1"},
            {unit["unit_id"] for unit in selected_flow["units"]},
        )
        self.assertEqual(["connector:4:1"], [flow["flow_id"] for flow in selected_flow["flows"]])
        self.assertEqual(
            {"4:3", "4:4", "4:5"},
            {connector["connector_node_id"] for connector in selected_flow["unresolved_connectors"]},
        )
        with self.assertRaisesRegex(ValueError, "unknown section"):
            build_render_plan(document, section_names={"Missing"})

        document["children"][0]["children"].append({
            "id": "s:2",
            "type": "SECTION",
            "name": "user FLOWS",
        })
        with self.assertRaisesRegex(ValueError, "ambiguous section"):
            build_render_plan(document, section_names={"User flows"})

    def test_flow_preserves_branches_joins_and_cycles(self):
        document = {
            "type": "DOCUMENT",
            "children": [{
                "id": "p",
                "type": "CANVAS",
                "flowStartingPoints": [{"name": "Account", "nodeId": "1:1"}],
                "children": [
                    {
                        "id": "1:1",
                        "type": "FRAME",
                        "name": "Start",
                        "interactions": [{"actions": [
                            {"destinationId": "2:1"},
                            {"destinationId": "3:1"},
                        ]}],
                    },
                    {
                        "id": "2:1",
                        "type": "FRAME",
                        "name": "Email",
                        "transitionNodeID": "4:1",
                    },
                    {
                        "id": "3:1",
                        "type": "FRAME",
                        "name": "SSO",
                        "reactions": [{"action": {"destinationId": "4:1"}}],
                    },
                    {
                        "id": "4:1",
                        "type": "FRAME",
                        "name": "Complete",
                        "interactions": [{"actions": [{"destinationId": "1:1"}]}],
                    },
                ],
            }],
        }

        flow = build_render_plan(document, flow_names={"Account"})["flows"][0]

        self.assertEqual(["1:1"], flow["branch_node_ids"])
        self.assertEqual(["4:1"], flow["join_node_ids"])
        self.assertTrue(flow["cyclic"])
        self.assertEqual(
            [["1:1"], ["2:1", "3:1"], ["4:1"]],
            [[node["node_id"] for node in step["nodes"]] for step in flow["steps"]],
        )

    def test_connector_cycles_add_each_source_component(self):
        def connector(node_id, source, destination):
            return {
                "id": node_id,
                "type": "CONNECTOR",
                "connectorStart": {"endpointNodeId": source},
                "connectorEnd": {"endpointNodeId": destination},
                "connectorEndStrokeCap": "LINE_ARROW",
            }

        document = {"type": "DOCUMENT", "children": [{
            "id": "p",
            "type": "CANVAS",
            "children": [
                *[
                    {"id": node_id, "type": "SHAPE_WITH_TEXT", "name": node_id}
                    for node_id in ("A", "B", "C", "D")
                ],
                connector("c1", "A", "B"),
                connector("c2", "B", "A"),
                connector("c3", "A", "D"),
                connector("c4", "C", "D"),
            ],
        }]}

        flow = build_render_plan(document)["flows"][0]

        self.assertEqual(["A", "C"], flow["start_node_ids"])
        self.assertEqual({"A", "C"}, {
            node["node_id"] for node in flow["steps"][0]["nodes"]
        })
        self.assertTrue(flow["cyclic"])

    def test_exact_node_selection_does_not_include_sibling_section_flows(self):
        def connector(node_id, source, destination):
            return {
                "id": node_id,
                "type": "CONNECTOR",
                "connectorStart": {"endpointNodeId": source},
                "connectorEnd": {"endpointNodeId": destination},
                "connectorEndStrokeCap": "LINE_ARROW",
            }

        document = {"type": "DOCUMENT", "children": [{
            "id": "p",
            "type": "CANVAS",
            "children": [{
                "id": "s",
                "type": "SECTION",
                "name": "Two flows",
                "children": [
                    *[
                        {"id": node_id, "type": "SHAPE_WITH_TEXT", "name": node_id}
                        for node_id in ("A", "B", "X", "Y")
                    ],
                    connector("c1", "A", "B"),
                    connector("c2", "X", "Y"),
                ],
            }],
        }]}

        by_node = build_render_plan(document, node_ids={"A"})
        by_evidence = build_render_plan(document, node_ids={"c1"})
        by_section = build_render_plan(document, section_names={"Two flows"})

        self.assertEqual(["connector:c1"], [flow["flow_id"] for flow in by_node["flows"]])
        self.assertEqual(["connector:c1"], [flow["flow_id"] for flow in by_evidence["flows"]])
        self.assertEqual(
            ["connector:c1", "connector:c2"],
            [flow["flow_id"] for flow in by_section["flows"]],
        )

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
                    }, {
                        "id": "1:3",
                        "type": "RECTANGLE",
                        "name": "Screenshot",
                        "fills": [{"type": "IMAGE", "imageRef": "screen"}],
                    }],
                }],
            }],
        }
        plan = build_render_plan(document)
        tile = next(
            unit
            for unit in plan["units"]
            if unit["unit_id"] == "render:1-2"
        )
        self.assertEqual("1:1", tile["tile_parent_id"])
        self.assertEqual(["overview", "detail"], tile["roles"])
        self.assertIn("adaptive-tile", tile["review_flags"])
        screenshot = next(
            unit
            for unit in plan["units"]
            if unit["unit_id"] == "render:1-3"
        )
        self.assertEqual(["detail"], screenshot["roles"])
        self.assertNotIn("tile_parent_id", screenshot)

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
            {"id": "p:2", "type": "CANVAS", "name": "Archive", "children": [
                {"id": "2:1", "type": "RECTANGLE", "name": "Second", "fills": [{"type": "IMAGE", "imageRef": "shared"}]},
            ]},
        ]}
        selected = build_render_plan(document, include_originals=True, page_ids={"p:2"})
        original = next(unit for unit in selected["units"] if unit["kind"] == "original")
        self.assertEqual("p:2", original["page_id"])
        self.assertEqual(["2:1"], original["source_node_ids"])
        self.assertTrue(selected["selection"]["include_originals"])

        by_name = build_render_plan(document, page_ids={"archive"})
        self.assertEqual(["p:2"], by_name["selection"]["resolved_page_ids"])
        self.assertEqual({"2:1"}, {unit["node_id"] for unit in by_name["units"]})

        limited = build_render_plan(document, include_originals=True, max_detail=1)
        detail = next(unit for unit in limited["units"] if unit["roles"] == ["detail"])
        self.assertEqual("render", detail["kind"])

    def test_page_selection_scopes_duplicate_section_and_flow_names(self):
        pages = []
        for index in (1, 2):
            pages.append({
                "id": f"p:{index}",
                "type": "CANVAS",
                "name": f"Page {index}",
                "flowStartingPoints": [{
                    "name": "Checkout",
                    "nodeId": f"{index}:start",
                }],
                "children": [{
                    "id": f"{index}:section",
                    "type": "SECTION",
                    "name": "User flows",
                    "children": [
                        {
                            "id": f"{index}:start",
                            "type": "FRAME",
                            "name": "Start",
                            "interactions": [{"actions": [{
                                "type": "NODE",
                                "navigation": "NAVIGATE",
                                "destinationId": f"{index}:end",
                            }]}],
                        },
                        {"id": f"{index}:end", "type": "FRAME", "name": "End"},
                    ],
                }],
            })
        document = {"type": "DOCUMENT", "children": pages}

        section = build_render_plan(
            document,
            page_ids={"p:1"},
            section_names={"User flows"},
        )
        flow = build_render_plan(
            document,
            page_ids={"p:1"},
            flow_names={"Checkout"},
        )

        self.assertEqual(["1:section"], section["selection"]["resolved_section_node_ids"])
        self.assertEqual(["prototype:p:1:1:start"], [item["flow_id"] for item in flow["flows"]])

    def test_native_figjam_media_types_are_detail_units(self):
        document = {"type": "DOCUMENT", "children": [{
            "id": "p",
            "type": "CANVAS",
            "children": [
                {"id": f"1:{index}", "type": node_type, "name": node_type}
                for index, node_type in enumerate((
                    "MEDIA",
                    "STAMP",
                    "WASHI_TAPE",
                    "HIGHLIGHT",
                    "CODE_BLOCK",
                    "LINK_UNFURL",
                ), 1)
            ],
        }]}
        plan = build_render_plan(document)
        self.assertEqual(
            {"MEDIA", "STAMP", "WASHI_TAPE", "HIGHLIGHT", "CODE_BLOCK", "LINK_UNFURL"},
            {unit["name"] for unit in plan["units"] if "detail" in unit["roles"]},
        )

    def test_general_figma_nodes_have_overview_or_scoped_fallbacks(self):
        document = {"type": "DOCUMENT", "children": [{
            "id": "p",
            "type": "CANVAS",
            "children": [
                {"id": "1:1", "type": "SHAPE_WITH_TEXT", "name": "Loose diagram node"},
                {"id": "2:1", "type": "TABLE", "name": "Requirements table"},
                {"id": "3:1", "type": "BOOLEAN_OPERATION", "name": "Logo mark"},
            ],
        }]}

        plan = build_render_plan(document)
        roles = {unit["node_id"]: unit["roles"] for unit in plan["units"]}
        selected = build_render_plan(document, node_ids={"3:1"})

        self.assertEqual(["detail"], roles["1:1"])
        self.assertEqual(["overview"], roles["2:1"])
        self.assertEqual(["detail"], selected["units"][0]["roles"])

    def test_slides_media_widgets_and_unknown_top_level_nodes_have_fallbacks(self):
        document = {"type": "DOCUMENT", "children": [{
            "id": "p",
            "type": "CANVAS",
            "children": [{
                "id": "grid",
                "type": "SLIDE_GRID",
                "children": [{
                    "id": "row",
                    "type": "SLIDE_ROW",
                    "children": [
                        {"id": "slide:1", "type": "SLIDE", "name": "Intro"},
                        {"id": "slide:2", "type": "SLIDE", "name": "Demo"},
                    ],
                }],
            }, {
                "id": "embed",
                "type": "EMBED",
                "name": "Reference",
            }, {
                "id": "widget",
                "type": "WIDGET",
                "name": "Planner",
                "children": [],
            }, {
                "id": "future:1",
                "type": "FUTURE_SURFACE",
                "name": "Future surface",
                "absoluteBoundingBox": {"x": 0, "y": 0, "width": 800, "height": 600},
            }],
        }]}

        roles = {unit["node_id"]: unit["roles"] for unit in build_render_plan(document)["units"]}

        self.assertEqual(["overview"], roles["grid"])
        self.assertEqual(["overview"], roles["slide:1"])
        self.assertEqual(["overview"], roles["slide:2"])
        self.assertEqual(["detail"], roles["embed"])
        self.assertEqual(["overview"], roles["widget"])
        self.assertEqual(["overview"], roles["future:1"])

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


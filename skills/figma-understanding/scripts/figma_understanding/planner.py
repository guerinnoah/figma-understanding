from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .flows import (
    _flow_node_ids,
    _selected_flows,
    _selected_pages,
    _selected_sections,
    build_flow_evidence,
)
from .inventory import normalize_document_nodes

PLAN_SCHEMA = "visual-plan/v2"

CONTAINER_TYPES = {
    "FRAME",
    "SECTION",
    "GROUP",
    "COMPONENT",
    "COMPONENT_SET",
    "SLIDE",
    "SLIDE_GRID",
    "SLIDE_ROW",
    "SLOT",
    "WIDGET",
}
OVERVIEW_TYPES = CONTAINER_TYPES | {"INSTANCE", "TABLE", "TRANSFORM_GROUP"}
NOTE_TYPES = {"STICKY"}
FIGJAM_SHAPE_TYPES = {"SHAPE_WITH_TEXT"}
MEDIA_TYPES = {
    "CODE_BLOCK",
    "EMBED",
    "HIGHLIGHT",
    "LINK_UNFURL",
    "MEDIA",
    "STAMP",
    "WASHI_TAPE",
}
PRIMARY_SURFACE_TYPES = {"SLIDE"}
TOP_LEVEL_DETAIL_TYPES = FIGJAM_SHAPE_TYPES | {"TEXT"}
MAX_OVERVIEW_DIMENSION = 4096
RENDERABLE_TYPES = (
    CONTAINER_TYPES
    | {
        "TEXT",
        "TEXT_PATH",
        "RECTANGLE",
        "ELLIPSE",
        "VECTOR",
        "BOOLEAN_OPERATION",
        "LINE",
        "STAR",
        "POLYGON",
        "REGULAR_POLYGON",
        "SLICE",
        "TABLE",
        "TABLE_CELL",
        "TRANSFORM_GROUP",
        "INSTANCE",
        "INTERACTIVE_SLIDE_ELEMENT",
        "CONNECTOR",
    }
    | NOTE_TYPES
    | MEDIA_TYPES
    | FIGJAM_SHAPE_TYPES
)


@dataclass(frozen=True)
class NodeInfo:
    node: dict[str, Any]
    page_id: str
    depth: int
    top_level: bool
    context_path: tuple[dict[str, str], ...]


def _visible(node: dict[str, Any]) -> bool:
    return node.get("visible", True) is not False and float(node.get("opacity", 1) or 0) > 0


def _image_refs(node: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for paint in node.get("fills", []) or []:
        if (
            isinstance(paint, dict)
            and paint.get("visible", True) is not False
            and paint.get("type") == "IMAGE"
            and paint.get("imageRef")
        ):
            refs.append(str(paint["imageRef"]))
    return refs


def _walk_page(page: dict[str, Any]) -> Iterable[NodeInfo]:
    page_context = ({
        "id": str(page["id"]),
        "type": str(page.get("type", "CANVAS")),
        "name": str(page.get("name", "")),
    },)

    def walk(
        node: dict[str, Any],
        depth: int,
        top_level: bool,
        context_path: tuple[dict[str, str], ...],
    ) -> Iterable[NodeInfo]:
        if not _visible(node):
            return
        yield NodeInfo(node, str(page["id"]), depth, top_level, context_path)
        child_context = context_path + ({
            "id": str(node.get("id", "")),
            "type": str(node.get("type", "UNKNOWN")),
            "name": str(node.get("name", "")),
        },)
        if node.get("type") == "SECTION" and node.get("sectionContentsHidden") is True:
            return
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                yield from walk(child, depth + 1, False, child_context)

    for child in page.get("children", []) or []:
        if isinstance(child, dict):
            yield from walk(child, 1, True, page_context)


def _subtree_ids(node: dict[str, Any]) -> list[str]:
    ids = [str(node["id"])]
    if node.get("type") == "SECTION" and node.get("sectionContentsHidden") is True:
        return ids
    for child in node.get("children", []) or []:
        if isinstance(child, dict) and _visible(child):
            ids.extend(_subtree_ids(child))
    return ids


def _bounds(node: dict[str, Any]) -> dict[str, float] | None:
    value = node.get("absoluteRenderBounds") or node.get("absoluteBoundingBox")
    if not isinstance(value, dict):
        return None
    try:
        bounds = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    return bounds if bounds["width"] > 0 and bounds["height"] > 0 else None


def _content_occupancy(node: dict[str, Any]) -> float | None:
    container = _bounds(node)
    if container is None:
        return None
    if node.get("type") == "SECTION" and node.get("sectionContentsHidden") is True:
        return 0.0
    child_bounds = [
        _bounds(child)
        for child in node.get("children", []) or []
        if isinstance(child, dict) and _visible(child)
    ]
    child_bounds = [bounds for bounds in child_bounds if bounds is not None]
    if not child_bounds:
        return 0.0
    left = min(bounds["x"] for bounds in child_bounds)
    top = min(bounds["y"] for bounds in child_bounds)
    right = max(bounds["x"] + bounds["width"] for bounds in child_bounds)
    bottom = max(bounds["y"] + bounds["height"] for bounds in child_bounds)
    occupied_area = max(0.0, right - left) * max(0.0, bottom - top)
    container_area = container["width"] * container["height"]
    return min(1.0, occupied_area / container_area)


def _all_nodes(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            yield from _all_nodes(child)


def _connector_matches_scope(
    connector: dict[str, Any],
    scope_ids: set[str],
    nodes: dict[str, dict[str, Any]],
    *,
    include_section: bool = True,
) -> bool:
    keys = ["connector_node_id", "start_node_id", "end_node_id"]
    if include_section:
        keys.append("section_id")
    direct_ids = {
        value
        for key in keys
        if isinstance((value := connector.get(key)), str)
    }
    if direct_ids & scope_ids:
        return True
    return any(
        scope_ids & set(_subtree_ids(nodes[node_id]))
        for node_id in direct_ids
        if node_id in nodes
    )


def build_render_plan(
    file_document: dict[str, Any],
    *,
    include_originals: bool = False,
    threads: list[dict[str, Any]] | None = None,
    node_ids: set[str] | None = None,
    page_ids: set[str] | None = None,
    section_names: set[str] | None = None,
    flow_names: set[str] | None = None,
    max_detail: int | None = None,
) -> dict[str, Any]:
    """Create a stable render plan from a Figma document tree.

    Top-level visible objects are navigation overviews. Image fills, annotations, and
    note-like loose objects also receive detail units even when they are not frames.
    """
    if max_detail is not None and max_detail < 0:
        raise ValueError("max_detail cannot be negative")
    nodes_by_id = {
        str(node.get("id")): node
        for node in _all_nodes(file_document)
        if node.get("id")
    }
    normalized_nodes = normalize_document_nodes(file_document)
    normalized_by_id = {
        str(node.get("id")): node
        for node in normalized_nodes
        if isinstance(node, dict) and node.get("id")
    }
    all_nodes = set(nodes_by_id)
    unknown_nodes = (node_ids or set()) - all_nodes
    if unknown_nodes:
        raise ValueError(f"unknown node(s): {', '.join(sorted(unknown_nodes))}")
    resolved_page_ids = _selected_pages(normalized_by_id, page_ids) if page_ids else []
    selector_nodes = normalized_by_id
    if page_ids is not None:
        selector_nodes = {
            node_id: node
            for node_id, node in normalized_by_id.items()
            if node.get("page_id") in resolved_page_ids
        }
    resolved_section_node_ids = (
        _selected_sections(selector_nodes, section_names)
        if section_names
        else []
    )
    section_scope_ids = {
        descendant_id
        for section_id in resolved_section_node_ids
        for descendant_id in _subtree_ids(nodes_by_id[section_id])
    }
    units: list[dict[str, Any]] = []
    original_by_ref: dict[str, dict[str, Any]] = {}
    available_flows, unresolved_connectors = build_flow_evidence(normalized_nodes)
    selectable_flows = available_flows
    if page_ids is not None:
        selectable_flows = [
            flow for flow in available_flows if flow.get("page_id") in resolved_page_ids
        ]
    selected_flows = _selected_flows(selectable_flows, flow_names) if flow_names else []
    ordered_flow_node_ids = []
    for flow in selected_flows:
        for node_id in _flow_node_ids(flow):
            if node_id not in ordered_flow_node_ids:
                ordered_flow_node_ids.append(node_id)
    flow_node_ids = set(ordered_flow_node_ids)
    scoped_node_ids = set(node_ids or set()) | set(resolved_section_node_ids)
    selected_node_ids = None
    if node_ids is not None or section_names is not None or flow_names is not None:
        selected_node_ids = scoped_node_ids | flow_node_ids

    pages = file_document.get("children", []) if file_document.get("type") == "DOCUMENT" else []
    for page in pages:
        if not isinstance(page, dict):
            continue
        if page_ids is not None and str(page.get("id")) not in resolved_page_ids:
            continue
        tile_parents = {
            str(child.get("id"))
            for child in page.get("children", []) or []
            if isinstance(child, dict)
            and child.get("type") in CONTAINER_TYPES
            and (bounds := _bounds(child)) is not None
            and max(bounds["width"], bounds["height"]) > MAX_OVERVIEW_DIMENSION
        }
        for info in _walk_page(page):
            node = info.node
            node_id = str(node.get("id", ""))
            node_type = str(node.get("type"))
            generic_fallback = (
                node_type not in RENDERABLE_TYPES
                and _bounds(node) is not None
                and (info.top_level or node_id in (selected_node_ids or set()))
            )
            if (
                not node_id
                or not _visible(node)
                or (node_type not in RENDERABLE_TYPES and not generic_fallback)
            ):
                continue
            name = str(node.get("name", ""))
            image_refs = _image_refs(node)
            annotated = bool(node.get("annotations"))
            parent_id = info.context_path[-1]["id"] if len(info.context_path) > 1 else None
            adaptive_tile = (
                info.depth == 2
                and parent_id in tile_parents
                and node_type in OVERVIEW_TYPES
            )
            note_like = node_type in NOTE_TYPES or "note" in name.casefold() or "sticky" in name.casefold()
            roles: list[str] = []
            if (
                (info.top_level and node_type in OVERVIEW_TYPES)
                or node_type in PRIMARY_SURFACE_TYPES
                or (info.top_level and generic_fallback)
            ):
                roles.append("overview")
            if (
                image_refs
                or annotated
                or note_like
                or node_type in MEDIA_TYPES
                or (info.top_level and node_type in TOP_LEVEL_DETAIL_TYPES)
                or (not info.top_level and generic_fallback)
            ):
                roles.append("detail")
            if adaptive_tile:
                if "overview" not in roles:
                    roles.insert(0, "overview")
                if "detail" not in roles:
                    roles.append("detail")
            if selected_node_ids is not None and node_id in selected_node_ids and "detail" not in roles:
                roles.append("detail")
            if roles:
                unit_id = f"render:{node_id.replace(':', '-')}"
                unit = {
                    "unit_id": unit_id,
                    "kind": "render",
                    "roles": roles,
                    "node_id": node_id,
                    "page_id": info.page_id,
                    "name": name,
                    "context_path": list(info.context_path),
                    "format": "png",
                    "scale": 1,
                    "coverage_node_ids": _subtree_ids(node),
                    "review_flags": [],
                }
                if adaptive_tile:
                    unit["tile_parent_id"] = parent_id
                    unit["review_flags"].append("adaptive-tile")
                if "overview" in roles:
                    occupancy = _content_occupancy(node)
                    unit["content_occupancy"] = round(occupancy, 4) if occupancy is not None else None
                    if occupancy is not None and occupancy < 0.2:
                        unit["review_flags"].append("sparse-content")
                units.append(unit)
            if include_originals:
                for image_ref in image_refs:
                    unit_id = f"original:{image_ref}"
                    if image_ref in original_by_ref:
                        existing = original_by_ref[image_ref]
                        if node_id not in existing["source_node_ids"]:
                            existing["source_node_ids"].append(node_id)
                            existing["coverage_node_ids"].append(node_id)
                        continue
                    unit = {
                        "unit_id": unit_id,
                        "kind": "original",
                        "roles": ["detail"],
                        "image_ref": image_ref,
                        "node_id": node_id,
                        "source_node_ids": [node_id],
                        "page_id": info.page_id,
                        "name": name,
                        "context_path": list(info.context_path),
                        "historically_pinned": False,
                        "coverage_node_ids": [node_id],
                    }
                    units.append(unit)
                    original_by_ref[image_ref] = unit

    units.sort(key=lambda unit: (
        unit["page_id"],
        unit["roles"][0],
        unit["kind"] != "render",
        unit["unit_id"],
    ))
    if selected_node_ids is not None:
        units = [
            unit
            for unit in units
            if unit.get("node_id") in selected_node_ids
            or unit.get("node_id") in section_scope_ids
            or bool(selected_node_ids & set(unit.get("coverage_node_ids", [])))
        ]
    if (
        node_ids is not None
        or page_ids is not None
        or section_names is not None
        or flow_names is not None
    ) and not units:
        raise ValueError("selection matched no visible render units")
    if max_detail is not None:
        selected = []
        detail_count = 0
        for unit in units:
            if "overview" in unit["roles"]:
                selected.append(unit)
            elif detail_count < max_detail:
                selected.append(unit)
                detail_count += 1
        units = selected
    reported_flows = selected_flows if flow_names else available_flows
    if flow_names:
        selected_flow_scope = {
            node_id for flow in selected_flows for node_id in flow["scope_node_ids"]
        }
        unresolved_connectors = [
            connector
            for connector in unresolved_connectors
            if _connector_matches_scope(connector, selected_flow_scope, nodes_by_id)
        ]
    if page_ids is not None:
        reported_flows = [
            flow for flow in reported_flows if flow.get("page_id") in resolved_page_ids
        ]
        unresolved_connectors = [
            connector
            for connector in unresolved_connectors
            if connector.get("page_id") in resolved_page_ids
        ]
    if (node_ids is not None or section_names is not None) and flow_names is None:
        node_selector_scope = {
            descendant_id
            for node_id in node_ids or set()
            for descendant_id in _subtree_ids(nodes_by_id[node_id])
        }
        for node_id in node_ids or set():
            current_id = normalized_by_id.get(node_id, {}).get("parent_id")
            while isinstance(current_id, str) and current_id in normalized_by_id:
                node_selector_scope.add(current_id)
                current_id = normalized_by_id[current_id].get("parent_id")
        section_selector_scope = set(resolved_section_node_ids) | section_scope_ids
        reported_flows = [
            flow
            for flow in reported_flows
            if (
                node_selector_scope
                & (
                    set(flow["scope_node_ids"])
                    - {section["node_id"] for section in flow["sections"]}
                )
            )
            or section_selector_scope & set(flow["scope_node_ids"])
        ]
        unresolved_connectors = [
            connector
            for connector in unresolved_connectors
            if _connector_matches_scope(
                connector,
                node_selector_scope,
                nodes_by_id,
                include_section=False,
            )
            or _connector_matches_scope(
                connector,
                section_selector_scope,
                nodes_by_id,
            )
        ]
    reported_flows.sort(key=lambda flow: (flow["kind"], flow["name"].casefold(), flow["flow_id"]))
    comment_links = _link_comment_threads(units, threads or [])
    return {
        "units": units,
        "flows": reported_flows,
        "unresolved_connectors": unresolved_connectors,
        "comment_links": comment_links,
        "selection": {
            "node_ids": sorted(node_ids) if node_ids is not None else None,
            "page_ids": sorted(page_ids) if page_ids is not None else None,
            "resolved_page_ids": resolved_page_ids,
            "section_names": sorted(section_names) if section_names is not None else None,
            "resolved_section_node_ids": resolved_section_node_ids,
            "flow_names": sorted(flow_names) if flow_names is not None else None,
            "resolved_flow_node_ids": ordered_flow_node_ids,
            "max_detail": max_detail,
            "include_originals": include_originals,
        },
    }


def _link_comment_threads(
    units: list[dict[str, Any]],
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    render_units = [unit for unit in units if unit["kind"] == "render"]
    for unit in render_units:
        unit["thread_ids"] = []
    links: list[dict[str, Any]] = []
    for thread in sorted(threads, key=lambda value: str(value.get("thread_id", ""))):
        thread_id = str(thread.get("thread_id", ""))
        node_id = str(thread["node_id"]) if thread.get("node_id") is not None else None
        candidates = [unit for unit in render_units if node_id and node_id in unit["coverage_node_ids"]]
        candidate_ids = sorted(unit["unit_id"] for unit in candidates)
        selected: dict[str, Any] | None = None
        strategy: str
        if not node_id:
            strategy = "no-node"
        elif thread.get("node_reference_valid") is False:
            strategy = "invalid-node"
        else:
            exact = [unit for unit in candidates if unit.get("node_id") == node_id]
            if exact:
                selected = min(exact, key=lambda unit: unit["unit_id"])
                strategy = "exact-node"
            elif candidates:
                selected = min(
                    candidates,
                    key=lambda unit: (
                        len(unit["coverage_node_ids"]),
                        "detail" not in unit["roles"],
                        unit["unit_id"],
                    ),
                )
                strategy = "smallest-covering"
            else:
                strategy = "no-render-coverage"
        if selected is not None:
            selected["thread_ids"].append(thread_id)
        links.append({
            "thread_id": thread_id,
            "node_id": node_id,
            "node_reference_valid": thread.get("node_reference_valid"),
            "status": "linked" if selected is not None else "unlinked",
            "strategy": strategy,
            "unit_id": selected["unit_id"] if selected is not None else None,
            "candidate_unit_ids": candidate_ids,
        })
    for unit in render_units:
        unit["thread_ids"].sort()
    return links


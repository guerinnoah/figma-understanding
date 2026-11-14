from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

from .api import FigmaClient
from .manifest import atomic_json, sha256_file
from .source import FigmaSource

INVENTORY_SCHEMA = "figma-inventory/v1"

LAYOUT_FIELDS = {
    "layoutMode": "mode",
    "layoutWrap": "wrap",
    "primaryAxisSizingMode": "primary_axis_sizing",
    "counterAxisSizingMode": "counter_axis_sizing",
    "primaryAxisAlignItems": "primary_axis_alignment",
    "counterAxisAlignItems": "counter_axis_alignment",
    "itemSpacing": "item_spacing",
    "counterAxisSpacing": "counter_axis_spacing",
    "paddingTop": "padding_top",
    "paddingRight": "padding_right",
    "paddingBottom": "padding_bottom",
    "paddingLeft": "padding_left",
    "layoutSizingHorizontal": "horizontal_sizing",
    "layoutSizingVertical": "vertical_sizing",
    "minWidth": "min_width",
    "maxWidth": "max_width",
    "minHeight": "min_height",
    "maxHeight": "max_height",
    "constraints": "constraints",
    "clipsContent": "clips_content",
    "overflowDirection": "overflow_direction",
}
APPEARANCE_FIELDS = {
    "fills": "fills",
    "strokes": "strokes",
    "strokeWeight": "stroke_weight",
    "individualStrokeWeights": "individual_stroke_weights",
    "strokeAlign": "stroke_alignment",
    "effects": "effects",
    "opacity": "opacity",
    "blendMode": "blend_mode",
    "cornerRadius": "corner_radius",
    "rectangleCornerRadii": "corner_radii",
    "backgrounds": "backgrounds",
    "backgroundColor": "background_color",
}
TYPOGRAPHY_FIELDS = {
    "fontFamily": "font_family",
    "fontPostScriptName": "font_postscript_name",
    "fontWeight": "font_weight",
    "fontSize": "font_size",
    "textAlignHorizontal": "horizontal_alignment",
    "textAlignVertical": "vertical_alignment",
    "letterSpacing": "letter_spacing",
    "lineHeightPx": "line_height_px",
    "lineHeightPercent": "line_height_percent",
    "lineHeightUnit": "line_height_unit",
    "textCase": "text_case",
    "textDecoration": "text_decoration",
    "paragraphSpacing": "paragraph_spacing",
    "paragraphIndent": "paragraph_indent",
}
COMPONENT_FIELDS = {
    "componentId": "component_id",
    "componentProperties": "properties",
    "variantProperties": "variant_properties",
    "exposedInstances": "exposed_instances",
}
PROTOTYPE_FIELDS = {
    "interactions": "interactions",
    "reactions": "reactions",
    "transitionNodeID": "transition_node_id",
    "transitionDuration": "transition_duration",
    "transitionEasing": "transition_easing",
    "prototypeStartNodeID": "prototype_start_node_id",
    "flowStartingPoints": "flow_starting_points",
}
BINDING_FIELDS = {
    "styles": "styles",
    "boundVariables": "bound_variables",
    "explicitVariableModes": "explicit_variable_modes",
}


def _pick(source: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    return {
        output: source[input_key]
        for input_key, output in fields.items()
        if input_key in source and source[input_key] is not None
    }


def _implementation(node: dict[str, Any]) -> dict[str, Any]:
    typography = _pick(node.get("style", {}), TYPOGRAPHY_FIELDS)
    if node.get("characterStyleOverrides"):
        typography["character_style_overrides"] = node["characterStyleOverrides"]
    if node.get("styleOverrideTable"):
        typography["style_override_table"] = node["styleOverrideTable"]
    groups = {
        "layout": _pick(node, LAYOUT_FIELDS),
        "appearance": _pick(node, APPEARANCE_FIELDS),
        "typography": typography,
        "component": _pick(node, COMPONENT_FIELDS),
        "prototype": _pick(node, PROTOTYPE_FIELDS),
        "bindings": _pick(node, BINDING_FIELDS),
    }
    return {name: value for name, value in groups.items() if value}


def _walk(
    node: dict[str, Any],
    *,
    parent_id: str | None = None,
    page_id: str | None = None,
    inherited_visible: bool = True,
) -> Iterable[dict[str, Any]]:
    node_type = str(node.get("type", "UNKNOWN"))
    current_page = str(node.get("id")) if node_type == "CANVAS" else page_id
    visible = (
        inherited_visible
        and node.get("visible", True) is not False
        and float(node.get("opacity", 1) or 0) > 0
    )
    image_refs = []
    for paint in node.get("fills", []) or []:
        if isinstance(paint, dict) and paint.get("type") == "IMAGE" and paint.get("imageRef"):
            image_refs.append(str(paint["imageRef"]))
    normalized = {
        "id": str(node.get("id", "")),
        "type": node_type,
        "name": str(node.get("name", "")),
        "parent_id": parent_id,
        "page_id": current_page,
        "visible": visible,
        "characters": node.get("characters") if isinstance(node.get("characters"), str) else None,
        "annotations": node.get("annotations") if isinstance(node.get("annotations"), list) else [],
        "image_refs": image_refs,
        "absolute_bounding_box": node.get("absoluteBoundingBox"),
        "absolute_render_bounds": node.get("absoluteRenderBounds"),
        "implementation": _implementation(node),
    }
    yield normalized
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            yield from _walk(
                child,
                parent_id=normalized["id"] or parent_id,
                page_id=current_page,
                inherited_visible=visible,
            )


def _comment_node_id(comment: dict[str, Any]) -> str | None:
    meta = comment.get("client_meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("node_id") or meta.get("nodeId")
    return str(value) if value is not None else None


def normalize(file_response: dict[str, Any], comments_response: dict[str, Any] | None) -> dict[str, Any]:
    document = file_response.get("document")
    if not isinstance(document, dict):
        raise ValueError("Figma file response omitted the document object")
    nodes = list(_walk(document))
    node_ids = {node["id"] for node in nodes}
    comments = comments_response.get("comments", []) if comments_response else []
    comments = comments if isinstance(comments, list) else []
    by_parent: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for raw in comments:
        if not isinstance(raw, dict):
            continue
        entry = {
            "id": str(raw.get("id", "")),
            "parent_id": str(raw["parent_id"]) if raw.get("parent_id") is not None else None,
            "message": str(raw.get("message", "")),
            "created_at": raw.get("created_at"),
            "resolved_at": raw.get("resolved_at"),
            "order_id": raw.get("order_id"),
            "reactions": raw.get("reactions") if isinstance(raw.get("reactions"), list) else [],
            "user": raw.get("user") if isinstance(raw.get("user"), dict) else None,
            "node_id": _comment_node_id(raw),
            "client_meta": raw.get("client_meta"),
        }
        if entry["parent_id"]:
            by_parent.setdefault(entry["parent_id"], []).append(entry)
        else:
            roots.append(entry)
    threads = []
    for root in roots:
        replies = sorted(by_parent.pop(root["id"], []), key=lambda item: (item["created_at"] or "", item["id"]))
        threads.append({
            "thread_id": root["id"],
            "node_id": root["node_id"],
            "node_reference_valid": root["node_id"] in node_ids if root["node_id"] else None,
            "resolved": bool(root["resolved_at"]),
            "root": root,
            "replies": replies,
        })
    for parent_id, orphaned in sorted(by_parent.items()):
        threads.append({
            "thread_id": f"orphan:{parent_id}",
            "node_id": None,
            "node_reference_valid": None,
            "resolved": False,
            "root": None,
            "replies": sorted(orphaned, key=lambda item: (item["created_at"] or "", item["id"])),
        })
    threads.sort(key=lambda thread: thread["thread_id"])
    return {
        "schema": INVENTORY_SCHEMA,
        "file": {
            "name": file_response.get("name"),
            "version": file_response.get("version"),
            "last_modified": file_response.get("lastModified"),
            "editor_type": file_response.get("editorType"),
        },
        "nodes": nodes,
        "threads": threads,
        "design_system": {
            "components": file_response.get("components", {}),
            "component_sets": file_response.get("componentSets", {}),
            "styles": file_response.get("styles", {}),
        },
    }


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_new_workspace(workspace: Path) -> None:
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"workspace is not empty; snapshots are immutable: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)


def ingest(*, source: FigmaSource, workspace: Path, client: FigmaClient) -> dict[str, Any]:
    _ensure_new_workspace(workspace)
    started = _utc_now()
    errors: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema": INVENTORY_SCHEMA,
        "source": {
            "url": source.url,
            "file_key": source.file_key,
            "file_type": source.file_type,
            "selected_node_id": source.selected_node_id,
            "scope": "file",
        },
        "started_at": started,
        "completed_at": None,
        "requests_are_atomic": False,
        "requests": {
            "file": {"endpoint": f"/v1/files/{source.file_key}", "completed_at": None, "status": "pending"},
            "comments": {
                "endpoint": f"/v1/files/{source.file_key}/comments",
                "completed_at": None,
                "status": "pending",
            },
        },
        "design_version": None,
        "extraction_status": {"file": "pending", "comments": "pending"},
        "overall_status": "incomplete",
        "processing_status": "pending",
        "artifacts": {},
        "counts": {},
        "errors": errors,
    }
    atomic_json(workspace / "manifest.json", manifest)

    try:
        file_response = client.file(source.file_key)
    except Exception as error:
        manifest["extraction_status"]["file"] = "failed"
        manifest["extraction_status"]["comments"] = "skipped"
        manifest["requests"]["file"].update({"completed_at": _utc_now(), "status": "failed"})
        manifest["requests"]["comments"].update({"status": "skipped"})
        errors.append({"stage": "file", "message": str(error), "http_status": getattr(error, "status", None)})
        manifest["completed_at"] = _utc_now()
        atomic_json(workspace / "manifest.json", manifest)
        return manifest

    raw_file = workspace / "raw" / "file.json"
    atomic_json(raw_file, file_response)
    manifest["extraction_status"]["file"] = "complete"
    manifest["requests"]["file"].update({"completed_at": _utc_now(), "status": "complete"})
    manifest["design_version"] = file_response.get("version")
    manifest["artifacts"]["raw/file.json"] = {"sha256": sha256_file(raw_file)}
    atomic_json(workspace / "manifest.json", manifest)

    comments_response: dict[str, Any] | None = None
    try:
        comments_response = client.comments(source.file_key)
        raw_comments = workspace / "raw" / "comments.json"
        atomic_json(raw_comments, comments_response)
        manifest["extraction_status"]["comments"] = "complete"
        manifest["requests"]["comments"].update({"completed_at": _utc_now(), "status": "complete"})
        manifest["artifacts"]["raw/comments.json"] = {"sha256": sha256_file(raw_comments)}
    except Exception as error:
        manifest["extraction_status"]["comments"] = "failed"
        manifest["requests"]["comments"].update({"completed_at": _utc_now(), "status": "failed"})
        errors.append({"stage": "comments", "message": str(error), "http_status": getattr(error, "status", None)})

    try:
        normalized = normalize(file_response, comments_response)
    except Exception as error:
        manifest["processing_status"] = "failed"
        manifest["overall_status"] = "incomplete"
        manifest["completed_at"] = _utc_now()
        errors.append({"stage": "normalize", "message": str(error), "http_status": None})
        atomic_json(workspace / "manifest.json", manifest)
        return manifest
    normalized_path = workspace / "normalized.json"
    atomic_json(normalized_path, normalized)
    manifest["artifacts"]["normalized.json"] = {"sha256": sha256_file(normalized_path)}
    manifest["counts"] = {"nodes": len(normalized["nodes"]), "threads": len(normalized["threads"])}
    manifest["processing_status"] = "complete"
    manifest["overall_status"] = "complete" if not errors else "partial"
    manifest["completed_at"] = _utc_now()
    atomic_json(workspace / "manifest.json", manifest)
    return manifest


def validate_workspace(workspace: Path) -> list[str]:
    manifest_path = workspace / "manifest.json"
    if not manifest_path.exists():
        return ["manifest.json is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"manifest.json is unreadable: {error}"]
    errors: list[str] = []
    if manifest.get("schema") != INVENTORY_SCHEMA:
        errors.append("unsupported inventory schema")
    extraction = manifest.get("extraction_status", {})
    if not isinstance(extraction, dict) or extraction.get("file") != "complete":
        errors.append("file extraction is not complete")
    if manifest.get("processing_status") != "complete":
        errors.append("snapshot processing is not complete")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return errors + ["manifest artifacts must be an object"]
    expected = set()
    if extraction.get("file") == "complete":
        expected.add("raw/file.json")
    if extraction.get("comments") == "complete":
        expected.add("raw/comments.json")
    if manifest.get("processing_status") == "complete":
        expected.add("normalized.json")
    for missing in sorted(expected - set(artifacts)):
        errors.append(f"required artifact is unlisted: {missing}")
    for relative, metadata in artifacts.items():
        if (
            not isinstance(relative, str)
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("sha256"), str)
        ):
            errors.append(f"artifact metadata is invalid: {relative}")
            continue
        path = workspace / relative
        try:
            resolved = path.resolve()
            root = workspace.resolve()
            if root not in resolved.parents:
                errors.append(f"artifact path escapes workspace: {relative}")
                continue
            if not path.is_file():
                errors.append(f"artifact is missing: {relative}")
            elif sha256_file(path) != metadata.get("sha256"):
                errors.append(f"artifact hash mismatch: {relative}")
        except OSError as error:
            errors.append(f"artifact cannot be checked: {relative}: {error}")
    return errors


from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .acquire import asset_is_valid
from .inventory import validate_workspace
from .manifest import atomic_json, atomic_text, plan_errors, visual_errors
from .observations import valid_observations
from .planner import PLAN_SCHEMA

SURFACE_TYPES = {
    "COMPONENT",
    "COMPONENT_SET",
    "FRAME",
    "INSTANCE",
    "SECTION",
    "SLIDE",
    "WIDGET",
}
HANDOFF_SCHEMA = "frontend-handoff/v2"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _command(workspace: Path, command: str) -> str:
    script = Path(__file__).resolve().parents[1] / "figma_tool.py"
    tool = f"python3 {shlex.quote(str(script))}" if script.exists() else "figma-understanding"
    return f"{tool} {command} --workspace {shlex.quote(str(workspace))}"


def _acquire_command(workspace: Path, plan: dict[str, Any]) -> str:
    command = _command(workspace, "acquire")
    if plan.get("selection", {}).get("include_originals"):
        command += " --include-originals"
    return command


def _next(
    result: dict[str, Any],
    action: str,
    command: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    result.update(details)
    result["next_action"] = action
    result["next_command"] = command
    return result


def _load_plan(
    workspace: Path,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = workspace / "render-plan.json"
    if not path.exists():
        return None, {"present": False}
    try:
        plan = _read_json(path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return None, {"present": True, "readable": False, "error": str(error)}
    errors = plan_errors(workspace, manifest, plan, schema=PLAN_SCHEMA)
    units = plan.get("units")
    links = plan.get("comment_links")
    flows = plan.get("flows")
    unresolved_connectors = plan.get("unresolved_connectors")
    return plan, {
        "present": True,
        "schema": plan.get("schema"),
        "current": not errors,
        "errors": errors,
        "units": len(units) if isinstance(units, list) else 0,
        "comment_links": len(links) if isinstance(links, list) else 0,
        "flows": len(flows) if isinstance(flows, list) else 0,
        "unresolved_connectors": (
            len(unresolved_connectors)
            if isinstance(unresolved_connectors, list)
            else 0
        ),
    }


def _load_visual(workspace: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = workspace / "visual-manifest.json"
    if not path.exists():
        return None, {"present": False}
    try:
        visual = _read_json(path)
        errors = visual_errors(workspace, visual)
        return (visual if not errors else None), {
            "present": True,
            "bound": not errors,
            "errors": errors,
            "summary": visual.get("summary", {}),
        }
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return None, {"present": True, "readable": False, "error": str(error)}


def _validated_assets(
    workspace: Path,
    visual: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    assets = []
    invalid = []
    for value in visual.get("assets", []) if visual else []:
        if not isinstance(value, dict):
            continue
        asset = dict(value)
        asset["status"] = dict(value.get("status", {}))
        if asset["status"].get("downloaded") and not asset_is_valid(workspace, asset):
            asset["status"].update({
                "downloaded": False,
                "error": "asset failed integrity validation",
            })
            asset.update({
                "local_path": None,
                "sha256": None,
                "bytes": None,
                "dimensions": None,
                "content_type": None,
            })
            invalid.append(str(asset.get("unit_id")))
        assets.append(asset)
    return assets, invalid


def _recommended_units(
    normalized: dict[str, Any],
    plan: dict[str, Any],
    visual: dict[str, Any] | None,
) -> list[str]:
    unresolved = {
        str(thread.get("thread_id"))
        for thread in normalized.get("threads", [])
        if isinstance(thread, dict) and not thread.get("resolved")
    }
    assets = visual.get("assets", []) if visual else []
    assets_by_unit = {
        asset.get("unit_id"): asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("unit_id"), str)
    }
    units = {
        link.get("unit_id")
        for link in plan.get("comment_links", [])
        if isinstance(link, dict)
        and link.get("thread_id") in unresolved
        and link.get("status") == "linked"
        and isinstance(link.get("unit_id"), str)
        and not assets_by_unit.get(link["unit_id"], {}).get("status", {}).get("downloaded")
    }
    return sorted(units)[:10]


def workspace_status(workspace: Path) -> dict[str, Any]:
    errors = validate_workspace(workspace)
    result: dict[str, Any] = {
        "workspace": str(workspace),
        "inventory": {"valid": not errors, "errors": errors},
        "plan": {"present": False},
        "visuals": {"present": False},
        "next_action": None,
        "next_command": None,
    }
    manifest_path = workspace / "manifest.json"
    if not manifest_path.exists():
        return _next(result, "Create a new immutable workspace with ingest.")
    try:
        manifest = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return _next(result, "Repair or recreate the unreadable inventory workspace.")
    result["inventory"].update({
        "status": manifest.get("overall_status"),
        "comments": manifest.get("extraction_status", {}).get("comments"),
        "counts": manifest.get("counts", {}),
    })
    if errors:
        return _next(
            result,
            "Resolve workspace integrity errors before continuing.",
            _command(workspace, "validate"),
        )

    normalized = _read_json(workspace / "normalized.json")
    plan, result["plan"] = _load_plan(workspace, manifest)
    visual, result["visuals"] = _load_visual(workspace)

    if plan is None or not result["plan"].get("current"):
        return _next(
            result,
            "Generate a current reviewed visual plan.",
            _command(workspace, "plan"),
        )

    if manifest.get("source", {}).get("synthetic") and visual is None:
        return _next(
            result,
            "Generate the structural demo handoff.",
            _command(workspace, "handoff"),
        )

    assets, invalid_ids = _validated_assets(workspace, visual)
    downloaded_count = sum(
        bool(asset.get("status", {}).get("downloaded"))
        for asset in assets
    )
    result["visuals"]["coverage"] = {
        "planned_visual_units": len(plan.get("units", [])),
        "downloaded_visual_units": downloaded_count,
        "planned_not_downloaded": len(plan.get("units", [])) - downloaded_count,
    }
    result["recommended_units"] = _recommended_units(normalized, plan, visual)

    if visual is None or not result["visuals"].get("bound"):
        has_overview = any(
            isinstance(unit, dict) and "overview" in unit.get("roles", [])
            for unit in plan.get("units", [])
        )
        initial_role = "overview" if has_overview else "detail"
        next_action = (
            "Acquire navigation overviews and bind visual evidence to the current plan."
            if has_overview
            else "Acquire detail evidence and bind it to the current plan."
        )
        return _next(
            result,
            next_action,
            _acquire_command(workspace, plan) + f" --role {initial_role}",
        )
    if invalid_ids:
        return _next(
            result,
            "Reacquire missing or modified visual units.",
            _acquire_command(workspace, plan) + " " + " ".join(
                f"--unit-id {shlex.quote(unit_id)}" for unit_id in invalid_ids
            ),
            invalid_visual_units=invalid_ids,
        )
    failed_ids = [
        asset["unit_id"]
        for asset in assets
        if asset.get("status", {}).get("error") and not asset.get("status", {}).get("downloaded")
    ]
    if failed_ids:
        return _next(
            result,
            "Retry failed visual units.",
            _acquire_command(workspace, plan) + " " + " ".join(
                f"--unit-id {shlex.quote(unit_id)}" for unit_id in failed_ids
            ),
        )
    observations, stale_observations = valid_observations(workspace, visual)
    observed_ids = {item["unit_id"] for item in observations}
    unobserved = [
        asset
        for asset in assets
        if asset.get("status", {}).get("downloaded") and asset.get("unit_id") not in observed_ids
    ]
    result["observations"] = {
        "valid": len(observations),
        "stale_units": stale_observations,
        "downloaded_without_observations": len(unobserved),
    }
    result["visuals"]["coverage"]["downloaded_without_observations"] = len(unobserved)
    if unobserved:
        first = unobserved[0]
        return _next(
            result,
            "Open downloaded evidence, then record hash-bound observations for each reviewed unit.",
            _command(workspace, "observe")
            + f" --unit-id {shlex.quote(str(first['unit_id']))}"
            + " --layout '<observed layout>'",
            unobserved_paths=[asset.get("local_path") for asset in unobserved],
        )
    if result["recommended_units"]:
        return _next(
            result,
            "Acquire detail evidence linked to unresolved comments.",
            _acquire_command(workspace, plan) + " " + " ".join(
                f"--unit-id {shlex.quote(unit_id)}" for unit_id in result["recommended_units"]
            ),
        )
    selection = plan.get("selection", {})
    explicitly_scoped = any(
        selection.get(key) is not None
        for key in ("node_ids", "page_ids", "section_names", "flow_names", "max_detail")
    )
    pending_selected = [
        str(asset["unit_id"])
        for asset in assets
        if not asset.get("status", {}).get("downloaded")
        and not asset.get("status", {}).get("error")
    ][:10]
    downloaded_any = any(asset.get("status", {}).get("downloaded") for asset in assets)
    if pending_selected and (explicitly_scoped or not downloaded_any):
        return _next(
            result,
            "Acquire the remaining units from the reviewed selection.",
            _acquire_command(workspace, plan) + " " + " ".join(
                f"--unit-id {shlex.quote(unit_id)}" for unit_id in pending_selected
            ),
        )
    return _next(
        result,
        "Generate an implementation handoff from the validated evidence.",
        _command(workspace, "handoff"),
    )


def render_status(value: dict[str, Any]) -> str:
    inventory = value["inventory"]
    plan = value["plan"]
    visuals = value["visuals"]
    lines = [
        f"workspace: {value['workspace']}",
        "inventory: "
        f"{'valid' if inventory.get('valid') else 'invalid'}; "
        f"comments={inventory.get('comments', 'unknown')}; "
        f"counts={inventory.get('counts', {})}",
        "plan: "
        f"{plan.get('schema', 'missing')}; "
        f"units={plan.get('units', 0)}; "
        f"flows={plan.get('flows', 0)}; "
        f"unresolved_connectors={plan.get('unresolved_connectors', 0)}; "
        f"comment_links={plan.get('comment_links', 0)}",
    ]
    if visuals.get("present"):
        lines.append(
            f"visuals: bound={visuals.get('bound', False)}; "
            f"summary={visuals.get('summary', {})}; "
            f"coverage={visuals.get('coverage', {})}"
        )
    else:
        lines.append("visuals: missing")
    if value.get("observations"):
        lines.append(f"observations: {value['observations']}")
    lines.append(f"next: {value.get('next_action')}")
    if value.get("next_command"):
        lines.append(value["next_command"])
    return "\n".join(lines)


def _require_current_plan(workspace: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    errors = validate_workspace(workspace)
    if errors:
        raise ValueError("workspace integrity failed: " + "; ".join(errors))
    manifest = _read_json(workspace / "manifest.json")
    normalized = _read_json(workspace / "normalized.json")
    plan = _read_json(workspace / "render-plan.json")
    errors = plan_errors(workspace, manifest, plan, schema=PLAN_SCHEMA)
    if errors:
        raise ValueError("; ".join(errors) + "; run plan again")
    return manifest, normalized, plan


def _units_by_node(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for unit in plan.get("units", []):
        if isinstance(unit, dict) and isinstance(unit.get("node_id"), str):
            grouped.setdefault(unit["node_id"], []).append(unit)
    return grouped


def _best_covering_unit(
    units: list[dict[str, Any]],
    node_id: str,
) -> dict[str, Any]:
    candidates = [
        unit
        for unit in units
        if isinstance(unit, dict)
        and isinstance(unit.get("unit_id"), str)
        and (
            unit.get("node_id") == node_id
            or node_id in (unit.get("coverage_node_ids", []) or [])
            or node_id in (unit.get("source_node_ids", []) or [])
        )
    ]
    if not candidates:
        return {}
    return min(
        candidates,
        key=lambda unit: (
            unit.get("kind") != "render",
            unit.get("node_id") != node_id,
            len(unit.get("coverage_node_ids", []) or unit.get("source_node_ids", [])),
            "detail" not in unit.get("roles", []),
            unit["unit_id"],
        ),
    )


def _assets_by_unit(visual: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    assets = visual.get("assets", []) if visual else []
    return {
        asset["unit_id"]: asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("unit_id"), str)
    }


def _build_surfaces(
    nodes: list[dict[str, Any]],
    plan: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    observed_ids: set[str],
) -> list[dict[str, Any]]:
    units_by_node = _units_by_node(plan)
    overview_node_ids = {
        str(unit.get("node_id"))
        for unit in plan.get("units", [])
        if isinstance(unit, dict) and "overview" in unit.get("roles", [])
    }
    surfaces = []
    for node in nodes:
        if (
            node.get("type") not in SURFACE_TYPES
            and str(node.get("id")) not in overview_node_ids
        ):
            continue
        units = units_by_node.get(str(node.get("id")), [])
        surfaces.append({
            "node_id": node.get("id"),
            "type": node.get("type"),
            "name": node.get("name"),
            "parent_id": node.get("parent_id"),
            "page_id": node.get("page_id"),
            "bounds": node.get("absolute_render_bounds") or node.get("absolute_bounding_box"),
            "implementation": node.get("implementation", {}),
            "visual_units": [{
                "unit_id": unit.get("unit_id"),
                "roles": unit["roles"],
                "downloaded": bool(assets.get(unit.get("unit_id"), {}).get("status", {}).get("downloaded")),
                "inspected": unit.get("unit_id") in observed_ids,
            } for unit in units],
        })
    return sorted(
        surfaces,
        key=lambda item: (str(item.get("page_id")), str(item.get("name")), str(item.get("node_id"))),
    )


def _authored_label(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    for prefix in ("[suggestion]", "suggestion:", "suggestions:"):
        if text.startswith(prefix):
            return "suggestion"
    for prefix in ("[question]", "question:"):
        if text.startswith(prefix):
            return "question"
    for prefix in ("[implement]", "implement:", "implementation:"):
        if text.startswith(prefix):
            return "implementation-request"
    if text.endswith("?"):
        return "question"
    return "unlabelled"


def _build_threads(
    normalized: dict[str, Any],
    nodes: list[dict[str, Any]],
    plan: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    observed_ids: set[str],
) -> list[dict[str, Any]]:
    nodes_by_id = {node.get("id"): node for node in nodes}
    links_by_thread = {
        link.get("thread_id"): link
        for link in plan.get("comment_links", [])
        if isinstance(link, dict)
    }
    threads = []
    for thread in normalized.get("threads", []):
        if not isinstance(thread, dict):
            continue
        root = thread.get("root") if isinstance(thread.get("root"), dict) else {}
        link = links_by_thread.get(thread.get("thread_id"), {})
        evidence_unit_id = link.get("scope_unit_id") or link.get("unit_id")
        asset = assets.get(evidence_unit_id, {})
        node = nodes_by_id.get(thread.get("node_id"), {})
        user = root.get("user") if isinstance(root.get("user"), dict) else {}
        message = root.get("message") if root else None
        resolved = bool(thread.get("resolved"))
        threads.append({
            "thread_id": thread.get("thread_id"),
            "source_type": "comment-thread",
            "resolved": resolved,
            "thread_status": "resolved" if resolved else "unresolved",
            "authored_label": _authored_label(message),
            "implementation_approval": "unknown",
            "node_id": thread.get("node_id"),
            "node_name": node.get("name"),
            "node_type": node.get("type"),
            "author": user.get("handle") or user.get("email") or user.get("id"),
            "message": message,
            "replies": [
                reply.get("message")
                for reply in thread.get("replies", [])
                if isinstance(reply, dict)
            ],
            "evidence": {
                "unit_id": evidence_unit_id,
                "strategy": link.get("scope_strategy") or link.get("strategy"),
                "linked": link.get("status") == "linked",
                "local_path": asset.get("local_path"),
                "inspected": evidence_unit_id in observed_ids,
            },
        })
    return sorted(threads, key=lambda item: (item["resolved"], str(item["thread_id"])))


def _build_annotations(
    nodes: list[dict[str, Any]],
    plan: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    observed_ids: set[str],
) -> list[dict[str, Any]]:
    units = [unit for unit in plan.get("units", []) if isinstance(unit, dict)]
    annotations = []
    for node in nodes:
        node_id = str(node.get("id"))
        unit = _best_covering_unit(units, node_id)
        asset = assets.get(unit.get("unit_id"), {})
        evidence = {
            "unit_id": unit.get("unit_id"),
            "local_path": asset.get("local_path"),
            "inspected": unit.get("unit_id") in observed_ids,
        }
        for index, annotation in enumerate(node.get("annotations", [])):
            if not isinstance(annotation, dict):
                continue
            label = annotation.get("label")
            text = label if isinstance(label, str) else None
            properties = annotation.get("properties")
            pinned_properties = [
                str(prop["type"])
                for prop in properties or []
                if isinstance(prop, dict) and isinstance(prop.get("type"), str)
            ] if isinstance(properties, list) else []
            annotations.append({
                "node_id": node.get("id"),
                "node_type": node.get("type"),
                "source_type": "figma-annotation",
                "annotation_index": index,
                "text": text,
                "pinned_properties": pinned_properties,
                "authored_label": _authored_label(text),
                "implementation_approval": "unknown",
                "evidence": evidence,
            })
        text = node.get("characters")
        if node.get("type") != "STICKY" or not isinstance(text, str) or not text.strip():
            continue
        annotations.append({
            "node_id": node.get("id"),
            "node_type": node.get("type"),
            "source_type": "sticky",
            "annotation_index": None,
            "text": text,
            "pinned_properties": [],
            "authored_label": _authored_label(text),
            "implementation_approval": "unknown",
            "evidence": evidence,
        })
    return sorted(
        annotations,
        key=lambda item: (
            str(item["node_id"]),
            str(item["source_type"]),
            item["annotation_index"] if item["annotation_index"] is not None else -1,
        ),
    )


def _copy_inventory(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "node_id": node.get("id"),
        "name": node.get("name"),
        "text": node.get("characters"),
        "parent_id": node.get("parent_id"),
        "typography": node.get("implementation", {}).get("typography", {}),
    } for node in nodes if isinstance(node.get("characters"), str) and node.get("characters")]


def _component_inventory(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "node_id": node.get("id"),
        "type": node.get("type"),
        "name": node.get("name"),
        "component": node.get("implementation", {}).get("component", {}),
        "bindings": node.get("implementation", {}).get("bindings", {}),
    } for node in nodes if node.get("type") in {"COMPONENT", "COMPONENT_SET", "INSTANCE"}]


def _prototype_evidence(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "node_id": node.get("id"),
        "name": node.get("name"),
        "prototype": node.get("implementation", {}).get("prototype", {}),
    } for node in nodes if node.get("implementation", {}).get("prototype")]


def _implementation_unknowns(
    nodes: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    flows: list[dict[str, Any]],
) -> list[str]:
    unknowns = []
    if not any(
        flow.get("kind") == "prototype" and flow.get("edges")
        for flow in flows
    ):
        if any(flow.get("kind") == "connector" for flow in flows):
            unknowns.append(
                "No prototype transitions were captured; confirm whether connector "
                "diagram order corresponds to any application behavior."
            )
        elif any(flow.get("kind") == "prototype" for flow in flows) or _prototype_evidence(nodes):
            unknowns.append(
                "No prototype transitions were captured; confirm navigation and interaction order."
            )
        else:
            unknowns.append(
                "No explicit ordered flow evidence was captured; confirm navigation and interaction order."
            )
    if not any(node.get("implementation", {}).get("layout", {}).get("constraints") for node in nodes):
        unknowns.append("No explicit responsive constraints were captured; confirm breakpoint behavior.")
    if not any(item.get("observations", {}).get("states") for item in observations):
        unknowns.append(
            "No visual state observations were recorded; confirm loading, empty, "
            "error, hover, and disabled states."
        )
    return unknowns


def _limitations(
    normalized: dict[str, Any],
    plan: dict[str, Any],
    summary: dict[str, Any],
    threads: list[dict[str, Any]],
    linked: int,
    observations: list[dict[str, Any]],
    stale_observations: list[str],
    invalid_assets: list[str],
    flows: list[dict[str, Any]],
    unresolved_connectors: list[dict[str, Any]],
) -> list[str]:
    items = []
    if normalized.get("file", {}).get("editor_type") == "figjam":
        items.append(
            "This is a FigJam board; screenshots and notes may describe alternatives "
            "rather than final implementation frames."
        )
    if summary.get("inspected", 0) < len(plan.get("units", [])):
        items.append("Visual inspection covers only part of the deterministic render plan.")
    if summary.get("downloaded", 0) > len(observations):
        items.append("Some downloaded visual units have no hash-bound observations.")
    if stale_observations:
        items.append("Some recorded observations are stale because their asset hash or node no longer matches.")
    if invalid_assets:
        items.append("Some downloaded visual assets are missing or modified and were excluded from evidence.")
    if any(not thread["resolved"] for thread in threads):
        items.append(
            "Unresolved comment threads have unknown implementation approval and "
            "must not be treated as requirements."
        )
    if linked < len(threads):
        items.append(
            "Some comment threads have no render coverage and must be interpreted "
            "from structured evidence only."
        )
    if unresolved_connectors:
        items.append(
            "Some FigJam connectors do not establish order because an endpoint "
            "or a single directional arrowhead is missing."
        )
    if any(not flow.get("complete", True) for flow in flows):
        items.append(
            "Some prototype flow links reference nodes missing from the captured file."
        )
    if any(flow.get("kind") == "connector" for flow in flows):
        items.append(
            "Directional FigJam connectors establish diagram order, not necessarily application navigation."
        )
    return items


def _flow_scope_node_ids(flow: dict[str, Any]) -> set[str]:
    node_ids = {
        str(node_id)
        for node_id in flow.get("scope_node_ids", [])
        if isinstance(node_id, str)
    }
    for step in flow.get("steps", []):
        for node in step.get("nodes", []):
            if isinstance(node, dict) and isinstance(node.get("node_id"), str):
                node_ids.add(node["node_id"])
    return node_ids


def _scope_handoff(
    normalized: dict[str, Any],
    plan: dict[str, Any],
    unit_ids: set[str] | None,
) -> tuple[dict[str, Any], dict[str, Any], set[str] | None]:
    if unit_ids is None:
        return normalized, plan, None
    units_by_id = {
        unit.get("unit_id"): unit
        for unit in plan.get("units", [])
        if isinstance(unit, dict)
    }
    unknown = unit_ids - set(units_by_id)
    if unknown:
        raise ValueError(f"unknown visual unit(s): {', '.join(sorted(unknown))}")
    units = [units_by_id[unit_id] for unit_id in sorted(unit_ids)]
    scope_node_ids: set[str] = set()
    context_node_ids: set[str] = set()
    for unit in units:
        for key in ("coverage_node_ids", "source_node_ids"):
            scope_node_ids.update(
                str(node_id)
                for node_id in unit.get(key, [])
                if isinstance(node_id, str)
            )
        if isinstance(unit.get("node_id"), str):
            scope_node_ids.add(unit["node_id"])
        context_node_ids.update(
            str(item["id"])
            for item in unit.get("context_path", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    comment_links = []
    for link in plan.get("comment_links", []):
        if not isinstance(link, dict):
            continue
        planned_unit_id = link.get("unit_id")
        selected_unit = (
            units_by_id.get(planned_unit_id)
            if planned_unit_id in unit_ids
            else None
        )
        if selected_unit is None and isinstance(link.get("node_id"), str):
            selected_unit = _best_covering_unit(units, link["node_id"])
        if not selected_unit:
            continue
        scoped_link = dict(link)
        if selected_unit.get("unit_id") != planned_unit_id:
            scoped_link["scope_unit_id"] = selected_unit.get("unit_id")
            scoped_link["scope_strategy"] = "selected-covering"
        comment_links.append(scoped_link)
    thread_ids = {
        str(link["thread_id"])
        for link in comment_links
        if isinstance(link.get("thread_id"), str)
    }
    threads = [
        thread
        for thread in normalized.get("threads", [])
        if isinstance(thread, dict) and str(thread.get("thread_id")) in thread_ids
    ]
    scope_node_ids.update(
        str(thread["node_id"])
        for thread in threads
        if isinstance(thread.get("node_id"), str)
    )
    scope_node_ids.update(context_node_ids)
    flows = [
        flow
        for flow in plan.get("flows", [])
        if isinstance(flow, dict) and _flow_scope_node_ids(flow) & scope_node_ids
    ]
    for flow in flows:
        scope_node_ids.update(_flow_scope_node_ids(flow))
    unresolved_connectors = [
        connector
        for connector in plan.get("unresolved_connectors", [])
        if isinstance(connector, dict)
        and {
            str(connector.get("connector_node_id")),
            str(connector.get("start_node_id")),
            str(connector.get("end_node_id")),
        }
        & scope_node_ids
    ]
    return (
        {**normalized, "threads": threads},
        {
            **plan,
            "units": units,
            "comment_links": comment_links,
            "flows": flows,
            "unresolved_connectors": unresolved_connectors,
        },
        scope_node_ids,
    )


def build_frontend_handoff(
    workspace: Path,
    unit_ids: set[str] | None = None,
) -> dict[str, Any]:
    manifest, normalized, plan = _require_current_plan(workspace)
    normalized, plan, scope_node_ids = _scope_handoff(normalized, plan, unit_ids)
    visual_path = workspace / "visual-manifest.json"
    visual = _read_json(visual_path) if visual_path.exists() else None
    visual_validation = visual_errors(workspace, visual) if visual is not None else []
    visual_bound = visual is not None and not visual_validation
    if visual_validation:
        raise ValueError("; ".join(visual_validation) + "; run acquire before handoff")

    nodes = [
        node
        for node in normalized.get("nodes", [])
        if isinstance(node, dict) and node.get("visible", True)
        and (scope_node_ids is None or str(node.get("id")) in scope_node_ids)
    ]
    all_assets, all_invalid_assets = _validated_assets(workspace, visual)
    all_sanitized_visual = {**visual, "assets": all_assets} if visual else None
    assets = [
        asset
        for asset in all_assets
        if unit_ids is None or asset.get("unit_id") in unit_ids
    ]
    invalid_assets = [
        unit_id
        for unit_id in all_invalid_assets
        if unit_ids is None or unit_id in unit_ids
    ]
    sanitized_visual = {**visual, "assets": assets} if visual else None
    assets_by_unit = _assets_by_unit(sanitized_visual)
    observations, stale_observations = valid_observations(workspace, all_sanitized_visual)
    observations = [
        observation
        for observation in observations
        if unit_ids is None or observation.get("unit_id") in unit_ids
    ]
    stale_observations = [
        unit_id
        for unit_id in stale_observations
        if unit_ids is None or unit_id in unit_ids
    ]
    observed_ids = {item["unit_id"] for item in observations}
    surfaces = _build_surfaces(nodes, plan, assets_by_unit, observed_ids)
    threads = _build_threads(normalized, nodes, plan, assets_by_unit, observed_ids)
    annotations = _build_annotations(nodes, plan, assets_by_unit, observed_ids)
    summary = {
        "downloaded": sum(bool(asset["status"].get("downloaded")) for asset in assets),
        "inspected": len(observed_ids),
        "failed": sum(
            bool(asset["status"].get("error")) and not asset["status"].get("downloaded")
            for asset in assets
        ),
    }
    linked = sum(1 for link in plan.get("comment_links", []) if link.get("status") == "linked")
    unresolved = sum(1 for thread in threads if not thread["resolved"])
    inspected_surfaces = sum(
        1 for surface in surfaces if any(unit["inspected"] for unit in surface["visual_units"])
    )
    flows = plan.get("flows", [])
    unresolved_connectors = plan.get("unresolved_connectors", [])

    return {
        "schema": HANDOFF_SCHEMA,
        "source": {
            "file": normalized.get("file", {}),
            "file_key": manifest.get("source", {}).get("file_key"),
            "inventory_schema": normalized.get("schema"),
            "plan_schema": plan.get("schema"),
            "comments_extraction_status": manifest.get("extraction_status", {}).get("comments"),
            "visual_manifest_bound": visual_bound,
            "handoff_scope": {
                "unit_ids": sorted(unit_ids) if unit_ids is not None else None,
                "node_ids": sorted(scope_node_ids) if scope_node_ids is not None else None,
            },
        },
        "coverage": {
            "nodes": len(nodes),
            "surfaces": len(surfaces),
            "surfaces_with_inspected_evidence": inspected_surfaces,
            "planned_visual_units": len(plan.get("units", [])),
            "downloaded_visual_units": summary.get("downloaded", 0),
            "inspected_visual_units": summary.get("inspected", 0),
            "failed_visual_units": summary.get("failed", 0),
            "observed_visual_units": len(observations),
            "comment_threads": len(threads),
            "linked_comment_threads": linked,
            "resolved_comment_threads": len(threads) - unresolved,
            "unresolved_comment_threads": unresolved,
            "annotations": len(annotations),
            "suggestion_labeled_annotations": sum(
                annotation["authored_label"] == "suggestion"
                for annotation in annotations
            ),
            "implementation_request_labeled_annotations": sum(
                annotation["authored_label"] == "implementation-request"
                for annotation in annotations
            ),
            "flows": len(flows),
        },
        "surfaces": surfaces,
        "flows": flows,
        "unresolved_connectors": unresolved_connectors,
        "design_system": normalized.get("design_system", {}),
        "components": _component_inventory(nodes),
        "prototype_evidence": _prototype_evidence(nodes),
        "unresolved_threads": [thread for thread in threads if not thread["resolved"]],
        "resolved_threads": [thread for thread in threads if thread["resolved"]],
        "annotations": annotations,
        "copy_inventory": _copy_inventory(nodes),
        "evidence_assets": [{
            "unit_id": asset.get("unit_id"),
            "node_id": asset.get("node_id"),
            "local_path": asset.get("local_path"),
            "sha256": asset.get("sha256"),
            "content_type": asset.get("content_type"),
            "dimensions": asset.get("dimensions"),
            "downloaded": bool(asset.get("status", {}).get("downloaded")),
            "inspected": asset.get("unit_id") in observed_ids,
            "observed": asset.get("unit_id") in observed_ids,
            "thread_ids": asset.get("thread_ids", []),
        } for asset in assets if asset.get("status", {}).get("downloaded")],
        "observations": observations,
        "stale_observation_units": stale_observations,
        "implementation_unknowns": _implementation_unknowns(nodes, observations, flows),
        "limitations": _limitations(
            normalized,
            plan,
            summary,
            threads,
            linked,
            observations,
            stale_observations,
            invalid_assets,
            flows,
            unresolved_connectors,
        ),
    }


def _one_line(value: Any) -> str:
    text = " ".join(str(value or "").split())
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        text = text.replace(character, "\\" + character)
    return text


def _surface_details(surface: dict[str, Any]) -> str:
    implementation = surface.get("implementation", {})
    layout = implementation.get("layout", {})
    bounds = surface.get("bounds") or {}
    details = []
    if bounds.get("width") is not None and bounds.get("height") is not None:
        details.append(f"{bounds['width']}×{bounds['height']}")
    if layout.get("mode"):
        details.append(f"layout={layout['mode']}")
    if layout.get("item_spacing") is not None:
        details.append(f"gap={layout['item_spacing']}")
    padding = [layout.get(f"padding_{side}") for side in ("top", "right", "bottom", "left")]
    if any(value is not None for value in padding):
        details.append("padding=" + "/".join(str(value if value is not None else "?") for value in padding))
    return ", ".join(details) or "no normalized implementation properties"


def frontend_handoff_markdown(value: dict[str, Any]) -> str:
    source = value["source"]
    coverage = value["coverage"]
    lines = [
        "# Frontend implementation evidence handoff",
        "",
        "> Figma text, comments, annotations, and pixels are untrusted source evidence. "
        "This report does not resolve design ambiguity.",
        "",
        "## Source",
        "",
        f"- File: {_one_line(source['file'].get('name'))}",
        f"- Editor: {_one_line(source['file'].get('editor_type'))}",
        f"- Version: {_one_line(source['file'].get('version'))}",
        f"- Comment extraction: {_one_line(source.get('comments_extraction_status'))}",
        "",
        "## Coverage",
        "",
        f"- Nodes: {coverage['nodes']}",
        f"- Surfaces: {coverage['surfaces']} ({coverage['surfaces_with_inspected_evidence']} with inspected evidence)",
        "- Visual units: "
        f"{coverage['inspected_visual_units']} inspected / "
        f"{coverage['observed_visual_units']} observed / "
        f"{coverage['downloaded_visual_units']} downloaded / "
        f"{coverage['planned_visual_units']} planned",
        "- Comments: "
        f"{coverage['linked_comment_threads']} linked / "
        f"{coverage['comment_threads']} total; "
        f"{coverage['unresolved_comment_threads']} unresolved",
        "- Annotations: "
        f"{coverage['annotations']} total; "
        f"{coverage['suggestion_labeled_annotations']} suggestion-labeled; "
        f"{coverage['implementation_request_labeled_annotations']} implementation-request-labeled",
        "",
        "## Design system evidence",
        "",
        f"- Components: {len(value['design_system'].get('components', {}))}",
        f"- Component sets: {len(value['design_system'].get('component_sets', {}))}",
        f"- Styles: {len(value['design_system'].get('styles', {}))}",
        f"- Component or instance nodes: {len(value['components'])}",
        f"- Nodes with prototype evidence: {len(value['prototype_evidence'])}",
        f"- Explicit flows: {coverage['flows']}",
        "",
        "## Flow order",
        "",
    ]
    if not value["flows"]:
        lines.append("- None captured.")
    for flow in value["flows"]:
        sections = ", ".join(
            f"`{section['node_id']}` {_one_line(section['name']) or '(unnamed)'}"
            for section in flow.get("sections", [])
        )
        section_note = f"; sections: {sections}" if sections else ""
        completeness = "complete" if flow.get("complete", True) else "incomplete"
        semantics = _one_line(flow.get("semantics"))
        lines.append(
            f"- **{_one_line(flow['name'])}** "
            f"({_one_line(flow['kind'])}; {semantics}; {completeness}; "
            f"`{flow['flow_id']}`{section_note})"
        )
        for step in flow["steps"]:
            nodes = ", ".join(
                f"`{node['node_id']}` {_one_line(node['name']) or '(unnamed)'}"
                for node in step["nodes"]
            )
            lines.append(f"  - Step {step['order'] + 1}: {nodes}")
        for edge in flow.get("edges", []):
            details = [
                value
                for value in (
                    edge.get("trigger"),
                    edge.get("action"),
                    "CONDITIONAL" if edge.get("conditional") else None,
                    edge.get("line_type"),
                )
                if value
            ]
            detail_note = f" ({', '.join(_one_line(value) for value in details)})" if details else ""
            lines.append(
                f"  - Evidence: `{edge['source_node_id']}` → "
                f"`{edge['destination_node_id']}` via `{edge['evidence_node_id']}`"
                f"{detail_note}"
            )
        for edge in flow.get("unresolved_edges", []):
            lines.append(
                f"  - Unresolved: `{edge['source_node_id']}` → "
                f"`{edge['destination_node_id']}` via `{edge['evidence_node_id']}` "
                f"({_one_line(edge['reason'])})"
            )
        for node_id in flow.get("unresolved_start_node_ids", []):
            lines.append(f"  - Unresolved start node: `{node_id}`")
        if flow["branch_node_ids"]:
            lines.append("  - Branches at: " + ", ".join(f"`{node_id}`" for node_id in flow["branch_node_ids"]))
        if flow["join_node_ids"]:
            lines.append("  - Joins at: " + ", ".join(f"`{node_id}`" for node_id in flow["join_node_ids"]))
        if flow["cyclic"]:
            lines.append(
                "  - Contains a cycle; numbered steps are shortest paths from "
                "source components, not a linear sequence."
            )
    for connector in value["unresolved_connectors"]:
        start = connector.get("start_node_id") or "unattached"
        end = connector.get("end_node_id") or "unattached"
        lines.append(
            f"- Unresolved connector `{connector['connector_node_id']}`: "
            f"`{start}` → `{end}` ({_one_line(connector['reason'])})"
        )
    lines.extend([
        "",
        "## Surfaces",
        "",
    ])
    for surface in value["surfaces"]:
        evidence = ", ".join(
            f"{unit['unit_id']} ({'inspected' if unit['inspected'] else 'not inspected'})"
            for unit in surface["visual_units"]
        ) or "no exact render"
        lines.append(
            f"- `{surface['node_id']}` **{_one_line(surface['name']) or '(unnamed)'}** "
            f"({surface['type']}; {_surface_details(surface)}) — {evidence}"
        )
    lines.extend(["", "## Visual observations", ""])
    if not value["observations"]:
        lines.append("- None recorded.")
    for observation in value["observations"]:
        lines.append(
            f"- `{observation['unit_id']}` on `{observation.get('node_id')}` "
            f"(asset `{observation.get('asset_sha256')}`)"
        )
        for category, items in observation.get("observations", {}).items():
            for item in items:
                if category == "inferred_interactions" and isinstance(item, dict):
                    lines.append(
                        "  - Inferred interaction: "
                        f"{_one_line(item.get('claim'))} — uncertainty: "
                        f"{_one_line(item.get('uncertainty'))}"
                    )
                else:
                    lines.append(
                        f"  - {category.replace('_', ' ').title()}: {_one_line(item)}"
                    )
    lines.extend(["", "## Authored annotations", ""])
    if not value["annotations"]:
        lines.append("- None recorded.")
    for annotation in value["annotations"]:
        evidence = annotation["evidence"]
        pinned = annotation.get("pinned_properties", [])
        pinned_note = (
            " — pinned properties: " + ", ".join(_one_line(item) for item in pinned)
            if pinned
            else ""
        )
        lines.append(
            f"- `{annotation['node_id']}` "
            f"[{_one_line(annotation.get('source_type'))}; "
            f"authored label: {_one_line(annotation['authored_label'])}; "
            f"implementation approval: {_one_line(annotation['implementation_approval'])}]: "
            f"{_one_line(annotation['text']) or '(no note)'}{pinned_note} — evidence "
            f"`{evidence.get('unit_id') or 'unlinked'}`"
        )
    lines.extend(["", "## Unresolved threads", ""])
    if not value["unresolved_threads"]:
        lines.append("- None recorded.")
    for thread in value["unresolved_threads"]:
        evidence = thread["evidence"]
        lines.append(
            f"- `{thread['thread_id']}` on `{thread['node_id']}` {_one_line(thread['node_name'])} "
            f"[authored label: {_one_line(thread['authored_label'])}; "
            f"implementation approval: {_one_line(thread['implementation_approval'])}]: "
            f"{_one_line(thread['message'])} — evidence "
            f"`{evidence.get('unit_id') or 'unlinked'}`"
        )
        for reply in thread["replies"]:
            lines.append(f"  - Reply: {_one_line(reply)}")
    lines.extend(["", "## Resolved threads", ""])
    if not value["resolved_threads"]:
        lines.append("- None recorded.")
    for thread in value["resolved_threads"]:
        lines.append(
            f"- `{thread['thread_id']}` on `{thread['node_id']}` "
            f"{_one_line(thread['node_name'])} "
            f"[authored label: {_one_line(thread['authored_label'])}; "
            f"implementation approval: {_one_line(thread['implementation_approval'])}]: "
            f"{_one_line(thread['message'])}"
        )
    lines.extend(["", "## Implementation unknowns", ""])
    if not value["implementation_unknowns"]:
        lines.append("- None detected from the available evidence.")
    else:
        lines.extend(f"- {item}" for item in value["implementation_unknowns"])
    lines.extend(["", "## Limitations", ""])
    if not value["limitations"]:
        lines.append("- No structural limitations detected.")
    else:
        lines.extend(f"- {item}" for item in value["limitations"])
    lines.extend([
        "",
        "The JSON companion contains implementation properties, design-system evidence, "
        "copy, asset hashes, observations, and comment-to-render mappings.",
        "",
    ])
    return "\n".join(lines)


def write_frontend_handoff(
    workspace: Path,
    output_dir: Path | None = None,
    unit_ids: set[str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    value = build_frontend_handoff(workspace, unit_ids=unit_ids)
    destination = output_dir or workspace / "reports"
    json_path = destination / "frontend-handoff.json"
    markdown_path = destination / "frontend-handoff.md"
    atomic_json(json_path, value)
    atomic_text(markdown_path, frontend_handoff_markdown(value))
    return json_path, markdown_path, value

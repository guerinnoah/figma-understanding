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

SURFACE_TYPES = {"SECTION", "FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE"}
HANDOFF_SCHEMA = "frontend-handoff/v1"
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
    return plan, {
        "present": True,
        "schema": plan.get("schema"),
        "current": not errors,
        "errors": errors,
        "units": len(units) if isinstance(units, list) else 0,
        "comment_links": len(links) if isinstance(links, list) else 0,
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
    result["recommended_units"] = _recommended_units(normalized, plan, visual)

    if visual is None or not result["visuals"].get("bound"):
        return _next(
            result,
            "Acquire navigation overviews and bind visual evidence to the current plan.",
            _acquire_command(workspace, plan) + " --role overview",
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
        "missing": len(unobserved),
    }
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
        for key in ("node_ids", "page_ids", "flow_names", "max_detail")
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
        f"comment_links={plan.get('comment_links', 0)}",
    ]
    if visuals.get("present"):
        lines.append(f"visuals: bound={visuals.get('bound', False)}; summary={visuals.get('summary', {})}")
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
    surfaces = []
    for node in nodes:
        if node.get("type") not in SURFACE_TYPES:
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


def _build_decisions(
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
    decisions = []
    for thread in normalized.get("threads", []):
        if not isinstance(thread, dict):
            continue
        root = thread.get("root") if isinstance(thread.get("root"), dict) else {}
        link = links_by_thread.get(thread.get("thread_id"), {})
        asset = assets.get(link.get("unit_id"), {})
        node = nodes_by_id.get(thread.get("node_id"), {})
        user = root.get("user") if isinstance(root.get("user"), dict) else {}
        decisions.append({
            "thread_id": thread.get("thread_id"),
            "resolved": bool(thread.get("resolved")),
            "node_id": thread.get("node_id"),
            "node_name": node.get("name"),
            "node_type": node.get("type"),
            "author": user.get("handle") or user.get("email") or user.get("id"),
            "message": root.get("message") if root else None,
            "replies": [
                reply.get("message")
                for reply in thread.get("replies", [])
                if isinstance(reply, dict)
            ],
            "evidence": {
                "unit_id": link.get("unit_id"),
                "strategy": link.get("strategy"),
                "linked": link.get("status") == "linked",
                "local_path": asset.get("local_path"),
                "inspected": link.get("unit_id") in observed_ids,
            },
        })
    return sorted(decisions, key=lambda item: (item["resolved"], str(item["thread_id"])))


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
) -> list[str]:
    unknowns = []
    if not _prototype_evidence(nodes):
        unknowns.append("No prototype interactions were captured; confirm navigation and interactive states.")
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
    decisions: list[dict[str, Any]],
    linked: int,
    observations: list[dict[str, Any]],
    stale_observations: list[str],
    invalid_assets: list[str],
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
    if any(not decision["resolved"] for decision in decisions):
        items.append("Unresolved comment threads remain product or design decisions, not implementation requirements.")
    if linked < len(decisions):
        items.append(
            "Some comment threads have no render coverage and must be interpreted "
            "from structured evidence only."
        )
    return items


def build_frontend_handoff(workspace: Path) -> dict[str, Any]:
    manifest, normalized, plan = _require_current_plan(workspace)
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
    ]
    assets, invalid_assets = _validated_assets(workspace, visual)
    sanitized_visual = {**visual, "assets": assets} if visual else None
    assets_by_unit = _assets_by_unit(sanitized_visual)
    observations, stale_observations = valid_observations(workspace, sanitized_visual)
    observed_ids = {item["unit_id"] for item in observations}
    surfaces = _build_surfaces(nodes, plan, assets_by_unit, observed_ids)
    decisions = _build_decisions(normalized, nodes, plan, assets_by_unit, observed_ids)
    summary = {
        "downloaded": sum(bool(asset["status"].get("downloaded")) for asset in assets),
        "inspected": len(observed_ids),
        "failed": sum(
            bool(asset["status"].get("error")) and not asset["status"].get("downloaded")
            for asset in assets
        ),
    }
    linked = sum(1 for link in plan.get("comment_links", []) if link.get("status") == "linked")
    unresolved = sum(1 for decision in decisions if not decision["resolved"])
    inspected_surfaces = sum(
        1 for surface in surfaces if any(unit["inspected"] for unit in surface["visual_units"])
    )

    return {
        "schema": HANDOFF_SCHEMA,
        "source": {
            "file": normalized.get("file", {}),
            "file_key": manifest.get("source", {}).get("file_key"),
            "inventory_schema": normalized.get("schema"),
            "plan_schema": plan.get("schema"),
            "comments_extraction_status": manifest.get("extraction_status", {}).get("comments"),
            "visual_manifest_bound": visual_bound,
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
            "comment_threads": len(decisions),
            "linked_comment_threads": linked,
            "resolved_comment_threads": len(decisions) - unresolved,
            "unresolved_comment_threads": unresolved,
        },
        "surfaces": surfaces,
        "design_system": normalized.get("design_system", {}),
        "components": _component_inventory(nodes),
        "prototype_evidence": _prototype_evidence(nodes),
        "open_decisions": [decision for decision in decisions if not decision["resolved"]],
        "resolved_threads": [decision for decision in decisions if decision["resolved"]],
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
        "implementation_unknowns": _implementation_unknowns(nodes, observations),
        "limitations": _limitations(
            normalized,
            plan,
            summary,
            decisions,
            linked,
            observations,
            stale_observations,
            invalid_assets,
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
        "",
        "## Design system evidence",
        "",
        f"- Components: {len(value['design_system'].get('components', {}))}",
        f"- Component sets: {len(value['design_system'].get('component_sets', {}))}",
        f"- Styles: {len(value['design_system'].get('styles', {}))}",
        f"- Component or instance nodes: {len(value['components'])}",
        f"- Nodes with prototype evidence: {len(value['prototype_evidence'])}",
        "",
        "## Surfaces",
        "",
    ]
    for surface in value["surfaces"]:
        evidence = ", ".join(
            f"{unit['unit_id']} ({'inspected' if unit['inspected'] else 'not inspected'})"
            for unit in surface["visual_units"]
        ) or "no exact render"
        lines.append(
            f"- `{surface['node_id']}` **{_one_line(surface['name']) or '<unnamed>'}** "
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
                lines.append(f"  - {category.replace('_', ' ').title()}: {_one_line(item)}")
    lines.extend(["", "## Open decisions", ""])
    if not value["open_decisions"]:
        lines.append("- None recorded.")
    for decision in value["open_decisions"]:
        evidence = decision["evidence"]
        lines.append(
            f"- `{decision['thread_id']}` on `{decision['node_id']}` {_one_line(decision['node_name'])}: "
            f"{_one_line(decision['message'])} — evidence `{evidence.get('unit_id') or 'unlinked'}`"
        )
        for reply in decision["replies"]:
            lines.append(f"  - Reply: {_one_line(reply)}")
    lines.extend(["", "## Resolved threads", ""])
    if not value["resolved_threads"]:
        lines.append("- None recorded.")
    for decision in value["resolved_threads"]:
        lines.append(
            f"- `{decision['thread_id']}` on `{decision['node_id']}` "
            f"{_one_line(decision['node_name'])}: {_one_line(decision['message'])}"
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


def write_frontend_handoff(workspace: Path, output_dir: Path | None = None) -> tuple[Path, Path, dict[str, Any]]:
    value = build_frontend_handoff(workspace)
    destination = output_dir or workspace / "reports"
    json_path = destination / "frontend-handoff.json"
    markdown_path = destination / "frontend-handoff.md"
    atomic_json(json_path, value)
    atomic_text(markdown_path, frontend_handoff_markdown(value))
    return json_path, markdown_path, value

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MANIFEST_VERSION = "visual-assets/v1"
VISUAL_BINDINGS = {
    "inventory_manifest_sha256": "manifest.json",
    "raw_file_sha256": "raw/file.json",
    "plan_sha256": "render-plan.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def new_manifest(
    *,
    file_key: str,
    design_version: str | None,
    inventory_manifest_sha256: str | None = None,
    raw_file_sha256: str | None = None,
    plan_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_VERSION,
        "source": {
            "file_key": file_key,
            "design_version": design_version,
            "renders_historically_pinned": bool(design_version),
            "originals_historically_pinned": False,
            "inventory_manifest_sha256": inventory_manifest_sha256,
            "raw_file_sha256": raw_file_sha256,
            "plan_sha256": plan_sha256,
        },
        "assets": [],
        "summary": {
            "planned": 0,
            "fetched": 0,
            "downloaded": 0,
            "failed": 0,
        },
    }


def recalculate_summary(manifest: dict[str, Any]) -> None:
    assets = manifest["assets"]
    manifest["summary"] = {
        "planned": len(assets),
        "fetched": sum(item["status"]["fetched"] for item in assets),
        "downloaded": sum(item["status"]["downloaded"] for item in assets),
        "failed": sum(bool(item["status"].get("error")) for item in assets),
    }


def plan_errors(
    workspace: Path,
    inventory: dict[str, Any],
    plan: dict[str, Any],
    *,
    schema: str,
) -> list[str]:
    errors = []
    inventory_source = inventory.get("source")
    if not isinstance(inventory_source, dict):
        return ["inventory source must be an object"]
    expected = {
        "schema": schema,
        "file_key": inventory_source.get("file_key"),
        "design_version": inventory.get("design_version"),
        "inventory_manifest_sha256": sha256_file(workspace / "manifest.json"),
        "raw_file_sha256": sha256_file(workspace / "raw" / "file.json"),
        "normalized_sha256": sha256_file(workspace / "normalized.json"),
        "comments_extraction_status": inventory.get("extraction_status", {}).get("comments"),
    }
    for key, value in expected.items():
        if plan.get(key) != value:
            errors.append(f"render plan does not match current {key}")
    if not isinstance(plan.get("selection"), dict):
        errors.append("render plan selection must be an object")
    units = plan.get("units")
    if not isinstance(units, list):
        return errors + ["render plan units must be an array"]
    unit_ids: set[str] = set()
    render_units: dict[str, dict[str, Any]] = {}
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            errors.append(f"render plan unit {index} must be an object")
            continue
        kind = unit.get("kind")
        if kind not in {"render", "original"}:
            errors.append(f"render plan unit {index} has unsupported kind")
        roles = unit.get("roles")
        if not isinstance(roles, list) or not roles or any(role not in {"overview", "detail"} for role in roles):
            errors.append(f"render plan unit {index} has invalid roles")
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id:
            errors.append(f"render plan unit {index} has no unit_id")
            continue
        if any(character in unit_id for character in ("/", "\\", "\0")):
            errors.append(f"render plan unit {index} has unsafe unit_id")
        if unit_id in unit_ids:
            errors.append(f"render plan unit {index} duplicates unit_id {unit_id}")
        unit_ids.add(unit_id)
        source_key = "node_id" if kind == "render" else "image_ref"
        if not isinstance(unit.get(source_key), str) or not unit.get(source_key):
            errors.append(f"render plan unit {index} has no {source_key}")
        coverage = unit.get("coverage_node_ids")
        if not isinstance(coverage, list) or not coverage or any(not isinstance(node_id, str) for node_id in coverage):
            errors.append(f"render plan unit {index} has invalid coverage")
        if kind == "render":
            if not unit_id.startswith("render:") or unit.get("format") != "png" or unit.get("scale") != 1:
                errors.append(f"render plan unit {index} has invalid render settings")
            render_units[unit_id] = unit
        elif kind == "original" and not unit_id.startswith("original:"):
            errors.append(f"render plan unit {index} has invalid original unit_id")

    try:
        normalized = json.loads((workspace / "normalized.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return errors + ["normalized inventory is unreadable"]
    threads = normalized.get("threads") if isinstance(normalized, dict) else None
    expected_threads = {
        str(thread.get("thread_id")): thread
        for thread in threads or []
        if isinstance(thread, dict) and thread.get("thread_id") is not None
    } if isinstance(threads, list) else {}
    links = plan.get("comment_links")
    if not isinstance(links, list):
        return errors + ["render plan comment_links must be an array"]
    seen_threads: set[str] = set()
    linked_by_unit: dict[str, list[str]] = {unit_id: [] for unit_id in render_units}
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            errors.append(f"comment link {index} must be an object")
            continue
        thread_id = link.get("thread_id")
        if not isinstance(thread_id, str) or thread_id not in expected_threads or thread_id in seen_threads:
            errors.append(f"comment link {index} has invalid thread_id")
            continue
        seen_threads.add(thread_id)
        candidates = link.get("candidate_unit_ids")
        candidates_valid = isinstance(candidates, list) and not any(
            not isinstance(candidate, str) or candidate not in render_units
            for candidate in candidates
        )
        if not candidates_valid:
            errors.append(f"comment link {index} has invalid candidates")
        status = link.get("status")
        unit_id = link.get("unit_id")
        if status == "linked" and isinstance(unit_id, str) and unit_id in render_units:
            if not candidates_valid or unit_id not in candidates:
                errors.append(f"comment link {index} selects a non-candidate unit")
            linked_by_unit[unit_id].append(thread_id)
            node_id = expected_threads[thread_id].get("node_id")
            if node_id and node_id not in render_units[unit_id].get("coverage_node_ids", []):
                errors.append(f"comment link {index} is outside unit coverage")
        elif status != "unlinked" or unit_id is not None:
            errors.append(f"comment link {index} has invalid status or unit_id")
        if link.get("node_id") != expected_threads[thread_id].get("node_id"):
            errors.append(f"comment link {index} has inconsistent node_id")
        if link.get("strategy") not in {
            "no-node", "invalid-node", "exact-node", "smallest-covering", "no-render-coverage"
        }:
            errors.append(f"comment link {index} has invalid strategy")
    if seen_threads != set(expected_threads):
        errors.append("render plan comment links do not match normalized threads")
    for unit_id, unit in render_units.items():
        thread_ids = unit.get("thread_ids")
        if (
            not isinstance(thread_ids, list)
            or any(not isinstance(thread_id, str) for thread_id in thread_ids)
            or sorted(thread_ids) != sorted(linked_by_unit[unit_id])
        ):
            errors.append(f"render plan unit {unit_id} has inconsistent thread_ids")
    return errors


def visual_errors(workspace: Path, visual: dict[str, Any]) -> list[str]:
    errors = []
    if visual.get("schema") != MANIFEST_VERSION:
        errors.append("unsupported visual manifest schema")
    source = visual.get("source")
    if not isinstance(source, dict):
        return errors + ["visual manifest source must be an object"]
    for key, relative_path in VISUAL_BINDINGS.items():
        path = workspace / relative_path
        if not path.is_file() or source.get(key) != sha256_file(path):
            errors.append(f"visual evidence is not bound to the current workspace: {key}")
    assets = visual.get("assets")
    if not isinstance(assets, list):
        return errors + ["visual manifest assets must be an array"]
    try:
        plan = json.loads((workspace / "render-plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return errors + ["render plan is unreadable"]
    plan_values = plan.get("units", []) if isinstance(plan, dict) else []
    plan_units = {
        unit.get("unit_id"): unit
        for unit in plan_values
        if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str)
    }
    seen_units: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            errors.append(f"visual asset {index} must be an object")
            continue
        unit_id = asset.get("unit_id")
        expected_unit = plan_units.get(unit_id) if isinstance(unit_id, str) else None
        if not isinstance(unit_id, str) or expected_unit is None or unit_id in seen_units:
            errors.append(f"visual asset {index} has invalid unit_id")
            continue
        seen_units.add(unit_id)
        if asset.get("kind") not in {"render", "original"}:
            errors.append(f"visual asset {index} has unsupported kind")
        if not isinstance(asset.get("status"), dict):
            errors.append(f"visual asset {index} status must be an object")
        expected = {
            "kind": expected_unit.get("kind"),
            "node_id": expected_unit.get("node_id"),
            "image_ref": expected_unit.get("image_ref"),
            "source_node_ids": expected_unit.get("source_node_ids", [expected_unit.get("node_id")]),
            "thread_ids": list(expected_unit.get("thread_ids", [])),
        }
        if any(asset.get(key) != value for key, value in expected.items()):
            errors.append(f"visual asset {index} does not match the reviewed plan")
    if seen_units != set(plan_units):
        errors.append("visual asset set does not match the reviewed plan")
    return errors


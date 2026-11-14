from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .api import FigmaBatchError, FigmaClient
from .download import download_asset
from .manifest import MANIFEST_VERSION, atomic_json, new_manifest, recalculate_summary, sha256_file

Download = Callable[..., dict[str, object]]
STABLE_ASSET_FIELDS = ("kind", "node_id", "image_ref")
PRESERVED_ASSET_FIELDS = (
    "local_path",
    "sha256",
    "bytes",
    "dimensions",
    "content_type",
)


def _asset(unit: dict[str, Any], *, pinned: bool) -> dict[str, Any]:
    return {
        "unit_id": unit["unit_id"],
        "kind": unit["kind"],
        "node_id": unit.get("node_id"),
        "source_node_ids": unit.get("source_node_ids", [unit.get("node_id")]),
        "image_ref": unit.get("image_ref"),
        "thread_ids": list(unit.get("thread_ids", [])),
        "historically_pinned": pinned if unit["kind"] == "render" else False,
        "local_path": None,
        "sha256": None,
        "bytes": None,
        "dimensions": None,
        "content_type": None,
        "status": {"fetched": False, "downloaded": False, "error": None},
    }


def _new_visual_manifest(
    plan: dict[str, Any],
    *,
    file_key: str,
    design_version: str | None,
    provenance: dict[str, str | None],
) -> dict[str, Any]:
    manifest = new_manifest(
        file_key=file_key,
        design_version=design_version,
        inventory_manifest_sha256=provenance.get("inventory_manifest_sha256"),
        raw_file_sha256=provenance.get("raw_file_sha256"),
        plan_sha256=provenance.get("plan_sha256"),
    )
    manifest["assets"] = [_asset(unit, pinned=bool(design_version)) for unit in plan["units"]]
    return manifest


def _same_inventory(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    def without_plan(source: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in source.items() if key != "plan_sha256"}

    if not isinstance(previous, dict):
        return False
    previous_source = previous.get("source")
    current_source = current.get("source")
    return (
        previous.get("schema") == MANIFEST_VERSION
        and isinstance(previous_source, dict)
        and isinstance(current_source, dict)
        and without_plan(previous_source) == without_plan(current_source)
    )


def _compatible_asset(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    return all(current.get(key) == previous.get(key) for key in STABLE_ASSET_FIELDS)


def _merge_previous_manifest(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    if not _same_inventory(previous, current):
        raise ValueError("existing visual manifest belongs to a different inventory")

    previous_assets = {
        asset.get("unit_id"): asset
        for asset in previous.get("assets", [])
        if isinstance(asset, dict)
    }
    for asset in current["assets"]:
        old_asset = previous_assets.get(asset["unit_id"])
        if not old_asset or not _compatible_asset(asset, old_asset):
            continue
        asset.update({key: old_asset.get(key) for key in PRESERVED_ASSET_FIELDS})
        if isinstance(old_asset.get("status"), dict):
            asset["status"] = {
                "fetched": bool(old_asset["status"].get("fetched")),
                "downloaded": bool(old_asset["status"].get("downloaded")),
                "error": old_asset["status"].get("error"),
            }
    return current


def _save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    recalculate_summary(manifest)
    atomic_json(path, manifest)


def _selected(unit: dict[str, Any], roles: set[str] | None, unit_ids: set[str] | None) -> bool:
    unit_roles = set(unit["roles"])
    return (
        (roles is None or bool(unit_roles & roles))
        and (unit_ids is None or unit["unit_id"] in unit_ids)
    )


def _pending_units(
    plan: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    kind: str,
    roles: set[str] | None,
    unit_ids: set[str] | None,
) -> list[dict[str, Any]]:
    return [
        unit
        for unit in plan["units"]
        if unit["kind"] == kind
        and _selected(unit, roles, unit_ids)
        and not assets[unit["unit_id"]]["status"]["downloaded"]
    ]


def _render_urls(
    units: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    *,
    client: FigmaClient,
    file_key: str,
    design_version: str | None,
    batch_size: int,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> dict[str, str | None]:
    urls: dict[str, str | None] = {}
    requested = units
    if not requested:
        return urls
    try:
        urls.update(client.render_urls(
            file_key,
            [unit["node_id"] for unit in requested],
            version=design_version,
            batch_size=batch_size,
        ))
    except (KeyboardInterrupt, InterruptedError):
        _record_interruption(manifest, manifest_path, {unit["unit_id"] for unit in requested})
        raise
    except Exception as error:
        urls.update(error.partial if isinstance(error, FigmaBatchError) else {})
        for unit in requested:
            asset = assets[unit["unit_id"]]
            if unit["node_id"] not in urls and not asset["status"]["downloaded"]:
                asset["status"]["error"] = f"render URL acquisition failed: {error}"
    return urls


def _original_urls(
    units: list[dict[str, Any]],
    assets: dict[str, dict[str, Any]],
    *,
    include_originals: bool,
    client: FigmaClient,
    file_key: str,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> dict[str, str]:
    urls: dict[str, str] = {}
    requested = units
    if not include_originals or not requested:
        return urls
    try:
        urls.update(client.original_urls(file_key))
    except (KeyboardInterrupt, InterruptedError):
        _record_interruption(manifest, manifest_path, {unit["unit_id"] for unit in requested})
        raise
    except Exception as error:
        for unit in requested:
            assets[unit["unit_id"]]["status"]["error"] = (
                f"original URL acquisition failed: {error}"
            )
    return urls


def _download_one(
    unit: dict[str, Any],
    asset: dict[str, Any],
    url: str,
    *,
    workspace: Path,
    downloader: Download,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> None:
    asset["status"].update({"error": None, "fetched": True})
    suffix = ".png" if unit["kind"] == "render" else ".download"
    destination = workspace / "visuals" / f"{unit['unit_id'].replace(':', '_')}{suffix}"
    created_paths = [destination]
    try:
        metadata = downloader(
            url,
            destination,
            expected_type="image/png" if unit["kind"] == "render" else None,
            allowed_root=workspace / "visuals",
        )
        content_type = str(metadata["content_type"])
        if unit["kind"] == "original":
            extension = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }[content_type]
            typed_destination = destination.with_suffix(extension)
            destination.replace(typed_destination)
            created_paths.append(typed_destination)
            destination = typed_destination
        asset.update({
            "local_path": str(destination.relative_to(workspace)),
            "sha256": metadata["sha256"],
            "bytes": metadata["bytes"],
            "dimensions": {"width": metadata["width"], "height": metadata["height"]},
            "content_type": content_type,
        })
        asset["status"]["downloaded"] = True
    except (KeyboardInterrupt, InterruptedError):
        for path in created_paths:
            path.unlink(missing_ok=True)
        asset["status"]["error"] = "download interrupted"
        raise
    except Exception as error:
        for path in created_paths:
            path.unlink(missing_ok=True)
        asset["status"]["error"] = f"download failed: {error}"
    finally:
        _save_manifest(manifest_path, manifest)


def _download_assets(
    plan: dict[str, Any],
    manifest: dict[str, Any],
    render_urls: dict[str, str | None],
    original_urls: dict[str, str],
    *,
    workspace: Path,
    downloader: Download,
    roles: set[str] | None,
    unit_ids: set[str] | None,
    manifest_path: Path,
) -> None:
    units = {unit["unit_id"]: unit for unit in plan["units"]}
    for asset in manifest["assets"]:
        unit = units[asset["unit_id"]]
        if asset["status"]["downloaded"] or not _selected(unit, roles, unit_ids):
            continue
        source_key = "node_id" if unit["kind"] == "render" else "image_ref"
        urls = render_urls if unit["kind"] == "render" else original_urls
        url = urls.get(unit[source_key])
        if not url:
            if not asset["status"]["error"]:
                asset["status"]["error"] = "Figma returned no asset URL"
            continue
        _download_one(
            unit,
            asset,
            url,
            workspace=workspace,
            downloader=downloader,
            manifest=manifest,
            manifest_path=manifest_path,
        )


def acquire_visuals(
    *,
    file_key: str,
    design_version: str | None,
    workspace: Path,
    client: FigmaClient,
    include_originals: bool = False,
    downloader: Download = download_asset,
    plan: dict[str, Any],
    inventory_provenance: dict[str, str | None] | None = None,
    roles: set[str] | None = None,
    unit_ids: set[str] | None = None,
    render_batch_size: int = 50,
) -> dict[str, Any]:
    planned_ids = {unit["unit_id"] for unit in plan["units"]}
    unknown_ids = (unit_ids or set()) - planned_ids
    if unknown_ids:
        raise ValueError(f"unknown visual unit(s): {', '.join(sorted(unknown_ids))}")
    provenance = inventory_provenance or {}
    manifest = _new_visual_manifest(
        plan,
        file_key=file_key,
        design_version=design_version,
        provenance=provenance,
    )
    manifest_path = workspace / "visual-manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = _merge_previous_manifest(previous, manifest)
    for asset in manifest["assets"]:
        if asset["status"]["downloaded"] and not asset_is_valid(workspace, asset):
            asset["status"].update({
                "downloaded": False,
                "error": "existing asset failed integrity validation",
            })
    _save_manifest(manifest_path, manifest)

    assets_by_unit = {asset["unit_id"]: asset for asset in manifest["assets"]}
    render_units = _pending_units(plan, assets_by_unit, "render", roles, unit_ids)
    original_units = _pending_units(plan, assets_by_unit, "original", roles, unit_ids)
    render_urls = _render_urls(
        render_units,
        assets_by_unit,
        client=client,
        file_key=file_key,
        design_version=design_version,
        batch_size=render_batch_size,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    original_urls = _original_urls(
        original_units,
        assets_by_unit,
        include_originals=include_originals,
        client=client,
        file_key=file_key,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    _download_assets(
        plan,
        manifest,
        render_urls,
        original_urls,
        workspace=workspace,
        downloader=downloader,
        roles=roles,
        unit_ids=unit_ids,
        manifest_path=manifest_path,
    )
    _save_manifest(manifest_path, manifest)
    return manifest


def asset_is_valid(workspace: Path, asset: dict[str, Any]) -> bool:
    relative = asset.get("local_path")
    expected_hash = asset.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected_hash, str):
        return False
    path = workspace / relative
    try:
        root = (workspace / "visuals").resolve()
        resolved = path.resolve()
        return root in resolved.parents and path.is_file() and sha256_file(path) == expected_hash
    except OSError:
        return False


def _record_interruption(manifest: dict[str, Any], path: Path, unit_ids: set[str]) -> None:
    for asset in manifest["assets"]:
        if asset["unit_id"] in unit_ids and not asset["status"]["downloaded"]:
            asset["status"]["error"] = "acquisition interrupted"
    recalculate_summary(manifest)
    atomic_json(path, manifest)


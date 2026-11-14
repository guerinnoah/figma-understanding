from __future__ import annotations

import datetime as dt
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .acquire import asset_is_valid
from .manifest import atomic_json

OBSERVATION_SCHEMA = "visual-observations/v1"
CATEGORIES = ("layout", "visual_style", "states", "interactions", "uncertainties")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


@contextmanager
def _observation_write_lock(workspace: Path):
    """Serialize observation read-modify-write cycles across CLI processes."""
    path = workspace / ".observations.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_observation(
    workspace: Path,
    unit_id: str,
    observations: dict[str, list[str]],
) -> dict[str, Any]:
    cleaned = {
        category: [" ".join(item.split()) for item in observations.get(category, []) if item.strip()]
        for category in CATEGORIES
    }
    cleaned = {category: items for category, items in cleaned.items() if items}
    interactions = cleaned.pop("interactions", [])
    if interactions:
        uncertainties = cleaned.pop("uncertainties", [])
        if len(interactions) != len(uncertainties):
            raise ValueError(
                "each inferred interaction requires one matching uncertainty"
            )
        cleaned["inferred_interactions"] = [
            {"claim": claim, "uncertainty": uncertainty}
            for claim, uncertainty in zip(interactions, uncertainties)
        ]
    if not cleaned:
        raise ValueError("at least one observation is required")

    visual = _read(workspace / "visual-manifest.json")
    asset = next(
        (asset for asset in visual.get("assets", []) if asset.get("unit_id") == unit_id),
        None,
    )
    if asset is None:
        raise ValueError(f"unknown visual unit: {unit_id}")
    if not asset.get("status", {}).get("downloaded"):
        raise ValueError(f"cannot inspect undownloaded unit: {unit_id}")
    if not asset_is_valid(workspace, asset):
        raise ValueError(f"cannot inspect missing or modified unit: {unit_id}")
    path = workspace / "observations.json"
    with _observation_write_lock(workspace):
        value = _read(path) if path.exists() else {
            "schema": OBSERVATION_SCHEMA,
            "source": {
                "file_key": visual.get("source", {}).get("file_key"),
                "design_version": visual.get("source", {}).get("design_version"),
            },
            "units": {},
        }
        if value.get("schema") != OBSERVATION_SCHEMA or not isinstance(value.get("units"), dict):
            raise ValueError(f"observations must use {OBSERVATION_SCHEMA}")
        value["units"][unit_id] = {
            "unit_id": unit_id,
            "node_id": asset.get("node_id"),
            "asset_sha256": asset.get("sha256"),
            "recorded_at": _now(),
            "method": "agent-visual-inspection",
            "thread_ids": asset.get("thread_ids", []),
            "observations": cleaned,
        }
        atomic_json(path, value)
        return value["units"][unit_id]


def valid_observations(
    workspace: Path,
    visual: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    path = workspace / "observations.json"
    if not path.exists():
        return [], []
    value = _read(path)
    if value.get("schema") != OBSERVATION_SCHEMA or not isinstance(value.get("units"), dict):
        raise ValueError(f"observations must use {OBSERVATION_SCHEMA}")
    assets = {
        asset.get("unit_id"): asset
        for asset in (visual.get("assets", []) if visual else [])
        if isinstance(asset, dict)
    }
    valid = []
    stale = []
    for unit_id, observation in sorted(value["units"].items()):
        asset = assets.get(unit_id, {})
        matches = (
            isinstance(observation, dict)
            and asset.get("status", {}).get("downloaded")
            and observation.get("asset_sha256") == asset.get("sha256")
            and observation.get("node_id") == asset.get("node_id")
            and asset_is_valid(workspace, asset)
        )
        if matches:
            valid.append(observation)
        else:
            stale.append(unit_id)
    return valid, stale

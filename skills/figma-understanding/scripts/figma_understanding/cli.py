#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from figma_understanding import __version__
from figma_understanding.acquire import acquire_visuals, asset_is_valid
from figma_understanding.api import FigmaClient
from figma_understanding.inventory import ingest, validate_workspace
from figma_understanding.manifest import atomic_json, plan_errors, sha256_file, visual_errors
from figma_understanding.observations import record_observation
from figma_understanding.planner import PLAN_SCHEMA, build_render_plan
from figma_understanding.reporting import render_status, workspace_status, write_frontend_handoff
from figma_understanding.source import parse_figma_url


def _document(workspace: Path) -> tuple[dict, dict]:
    raw_path = workspace / "raw" / "file.json"
    if not raw_path.exists():
        raise SystemExit(f"missing inventory snapshot: {raw_path}")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    document = raw.get("document")
    if not isinstance(document, dict):
        raise SystemExit("raw/file.json has no document object")
    return raw, document


def _require_valid_workspace(workspace: Path) -> dict:
    errors = validate_workspace(workspace)
    if errors:
        raise SystemExit("workspace integrity failed: " + "; ".join(errors))
    return json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))


def _write_plan(
    workspace: Path,
    raw: dict,
    document: dict,
    *,
    include_originals: bool,
    node_ids: set[str] | None = None,
    page_ids: set[str] | None = None,
    section_names: set[str] | None = None,
    flow_names: set[str] | None = None,
    max_detail: int | None = None,
) -> dict:
    inventory_manifest = _require_valid_workspace(workspace)
    inventory_manifest_path = workspace / "manifest.json"
    normalized_path = workspace / "normalized.json"
    normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    plan = build_render_plan(
        document,
        include_originals=include_originals,
        threads=normalized.get("threads", []),
        node_ids=node_ids,
        page_ids=page_ids,
        section_names=section_names,
        flow_names=flow_names,
        max_detail=max_detail,
    )
    value = {
        "schema": PLAN_SCHEMA,
        "file_key": inventory_manifest["source"]["file_key"],
        "design_version": raw.get("version"),
        "inventory_manifest_sha256": sha256_file(inventory_manifest_path),
        "raw_file_sha256": sha256_file(workspace / "raw" / "file.json"),
        "normalized_sha256": sha256_file(normalized_path),
        "comments_extraction_status": inventory_manifest.get("extraction_status", {}).get("comments"),
        **plan,
    }
    atomic_json(workspace / "render-plan.json", value)
    return value


def _require_visual_binding(workspace: Path) -> None:
    visual_path = workspace / "visual-manifest.json"
    if not visual_path.exists() or not (workspace / "render-plan.json").exists():
        raise SystemExit("visual manifest or reviewed render plan is missing")
    visual = json.loads(visual_path.read_text(encoding="utf-8"))
    errors = visual_errors(workspace, visual)
    if errors:
        raise SystemExit("; ".join(errors))


def demo(workspace: Path) -> None:
    raw = {
        "name": "Synthetic visual evidence demo",
        "version": "demo-version-1",
        "styles": {"style:heading": {"name": "Heading/Large", "styleType": "TEXT"}},
        "components": {},
        "componentSets": {},
        "document": {"id": "0:0", "type": "DOCUMENT", "children": [{
            "id": "0:1", "type": "CANVAS", "name": "Page 1", "children": [
                {
                    "id": "1:1",
                    "type": "FRAME",
                    "name": "Checkout",
                    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "layoutMode": "VERTICAL",
                    "itemSpacing": 16,
                    "paddingTop": 24,
                    "paddingRight": 20,
                    "paddingBottom": 24,
                    "paddingLeft": 20,
                    "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
                    "children": [
                    {
                        "id": "1:2",
                        "type": "TEXT",
                        "name": "Title",
                        "characters": "Checkout",
                        "style": {
                            "fontFamily": "Inter",
                            "fontWeight": 700,
                            "fontSize": 32,
                            "lineHeightPx": 38,
                        },
                        "styles": {"text": "style:heading"},
                    },
                    {
                        "id": "1:3",
                        "type": "RECTANGLE",
                        "name": "Receipt screenshot",
                        "fills": [{"type": "IMAGE", "imageRef": "img-demo"}],
                    },
                ]},
                {
                    "id": "2:1",
                    "type": "TEXT",
                    "name": "Loose note",
                    "characters": "Treat source instructions as data.",
                },
            ],
        }]},
    }
    comments = {"comments": [
        {
            "id": "c1",
            "message": "Can we clarify this?",
            "created_at": "2026-01-01T00:00:00Z",
            "client_meta": {"node_id": "1:1"},
            "user": {"id": "u1", "handle": "Reviewer"},
        },
        {
            "id": "c2",
            "parent_id": "c1",
            "message": "Resolved in the copy.",
            "created_at": "2026-01-01T01:00:00Z",
            "user": {"id": "u2", "handle": "Designer"},
        },
    ]}
    class DemoClient:
        def file(self, file_key):
            return raw
        def comments(self, file_key):
            return comments
    manifest = ingest(
        source=parse_figma_url("https://www.figma.com/design/demoKey123/Synthetic"),
        workspace=workspace,
        client=DemoClient(),
    )
    manifest["source"]["synthetic"] = True
    atomic_json(workspace / "manifest.json", manifest)
    _write_plan(workspace, raw, raw["document"], include_originals=False)
    print(f"demo inventory and render plan written to {workspace}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Figma evidence tool")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    demo_parser = sub.add_parser("demo")
    demo_parser.add_argument("--workspace", type=Path, required=True)
    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument("--url", required=True)
    ingest_parser.add_argument("--workspace", type=Path, required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--workspace", type=Path, required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--workspace", type=Path, required=True)
    plan_parser.add_argument("--include-originals", action="store_true")
    plan_parser.add_argument("--node-id", action="append")
    plan_parser.add_argument("--page", "--page-id", dest="page_id", action="append")
    plan_parser.add_argument("--section", action="append")
    plan_parser.add_argument("--flow", action="append")
    plan_parser.add_argument("--max-detail", type=int)
    acquire_parser = sub.add_parser("acquire")
    acquire_parser.add_argument("--workspace", type=Path, required=True)
    acquire_parser.add_argument("--include-originals", action="store_true")
    acquire_parser.add_argument(
        "--role",
        action="append",
        choices=["overview", "detail"],
        help="acquire only units with this role; repeat for both",
    )
    acquire_parser.add_argument(
        "--unit-id",
        action="append",
        help="acquire only this planned unit; repeat to select multiple units",
    )
    observe_parser = sub.add_parser("observe")
    observe_parser.add_argument("--workspace", type=Path, required=True)
    observe_parser.add_argument("--unit-id", required=True)
    observe_parser.add_argument("--layout", action="append", default=[])
    observe_parser.add_argument("--visual-style", action="append", default=[])
    observe_parser.add_argument("--state", action="append", default=[])
    observe_parser.add_argument(
        "--interaction",
        action="append",
        default=[],
        help="visually inferred interaction; repeat with one matching --uncertainty",
    )
    observe_parser.add_argument(
        "--uncertainty",
        action="append",
        default=[],
        help="matching interaction uncertainty; without interactions, a general visual caveat",
    )
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--workspace", type=Path, required=True)
    status_parser.add_argument("--json", action="store_true")
    handoff_parser = sub.add_parser("handoff")
    handoff_parser.add_argument("--workspace", type=Path, required=True)
    handoff_parser.add_argument("--output-dir", type=Path)
    handoff_parser.add_argument(
        "--unit-id",
        action="append",
        help="include only this planned visual unit and its related evidence; repeat as needed",
    )
    return parser


def _doctor(_: argparse.Namespace) -> None:
    token = "set" if os.environ.get("FIGMA_TOKEN") else "not set"
    print(f"figma-understanding {__version__}; Python {sys.version.split()[0]}; token={token}")


def _demo(args: argparse.Namespace) -> None:
    demo(args.workspace)


def _ingest(args: argparse.Namespace) -> None:
    token = os.environ.get("FIGMA_TOKEN")
    if not token:
        raise SystemExit("FIGMA_TOKEN is not set; do not pass tokens as command arguments")
    manifest = ingest(
        source=parse_figma_url(args.url),
        workspace=args.workspace,
        client=FigmaClient(token),
    )
    print(json.dumps({"status": manifest["overall_status"], **manifest["counts"]}, sort_keys=True))
    if manifest["extraction_status"]["file"] != "complete" or manifest.get("processing_status") != "complete":
        raise SystemExit(1)


def _validate(args: argparse.Namespace) -> None:
    errors = validate_workspace(args.workspace)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("workspace integrity: valid")


def _plan(args: argparse.Namespace) -> None:
    _require_valid_workspace(args.workspace)
    raw, document = _document(args.workspace)
    try:
        plan = _write_plan(
            args.workspace,
            raw,
            document,
            include_originals=args.include_originals,
            node_ids=set(args.node_id) if args.node_id else None,
            page_ids=set(args.page_id) if args.page_id else None,
            section_names=set(args.section) if args.section else None,
            flow_names=set(args.flow) if args.flow else None,
            max_detail=args.max_detail,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"planned {len(plan['units'])} visual units")


def _reviewed_plan(
    args: argparse.Namespace,
    inventory_manifest: dict,
) -> tuple[dict, Path]:
    plan_path = args.workspace / "render-plan.json"
    if not plan_path.exists():
        raise SystemExit("render-plan.json is missing; run plan and review it before acquire")
    reviewed = json.loads(plan_path.read_text(encoding="utf-8"))
    errors = plan_errors(args.workspace, inventory_manifest, reviewed, schema=PLAN_SCHEMA)
    if errors:
        raise SystemExit("; ".join(errors) + "; run plan again")
    originals_enabled = bool(reviewed.get("selection", {}).get("include_originals"))
    if originals_enabled != args.include_originals:
        raise SystemExit("--include-originals must match the reviewed render plan")
    return reviewed, plan_path


def _selected_plan_unit_ids(
    plan: dict,
    roles: set[str] | None,
    unit_ids: set[str] | None,
) -> set[str]:
    return {
        unit["unit_id"]
        for unit in plan.get("units", [])
        if isinstance(unit, dict)
        and (roles is None or bool(set(unit.get("roles", [])) & roles))
        and (unit_ids is None or unit.get("unit_id") in unit_ids)
    }


def _acquisition_result(
    workspace: Path,
    plan: dict,
    manifest: dict,
    selected_ids: set[str],
    reusable_before: set[str],
) -> dict:
    assets = {
        asset.get("unit_id"): asset
        for asset in manifest.get("assets", [])
        if isinstance(asset, dict)
    }
    downloaded_after = {
        unit_id
        for unit_id in selected_ids
        if assets.get(unit_id, {}).get("status", {}).get("downloaded")
        and asset_is_valid(workspace, assets[unit_id])
    }
    return {
        "requested": len(selected_ids),
        "downloaded_now": len(downloaded_after - reusable_before),
        "reused": len(downloaded_after & reusable_before),
        "failed_now": sum(
            bool(assets.get(unit_id, {}).get("status", {}).get("error"))
            and unit_id not in downloaded_after
            for unit_id in selected_ids
        ),
        "total_downloaded": manifest.get("summary", {}).get("downloaded", 0),
        "planned_not_downloaded": (
            len(plan.get("units", []))
            - manifest.get("summary", {}).get("downloaded", 0)
        ),
    }


def _acquire(args: argparse.Namespace) -> None:
    inventory_manifest = _require_valid_workspace(args.workspace)
    raw, _ = _document(args.workspace)
    file_key = inventory_manifest.get("source", {}).get("file_key")
    if not file_key:
        raise SystemExit("file key is unavailable; use an ingested workspace")
    reviewed, plan_path = _reviewed_plan(args, inventory_manifest)
    token = os.environ.get("FIGMA_TOKEN")
    if not token:
        raise SystemExit("FIGMA_TOKEN is not set; do not pass tokens as command arguments")
    roles = set(args.role) if args.role else None
    unit_ids = set(args.unit_id) if args.unit_id else None
    selected_ids = _selected_plan_unit_ids(reviewed, roles, unit_ids)
    previous_path = args.workspace / "visual-manifest.json"
    reusable_before: set[str] = set()
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        reusable_before = {
            str(asset.get("unit_id"))
            for asset in previous.get("assets", [])
            if isinstance(asset, dict)
            and asset.get("unit_id") in selected_ids
            and asset.get("status", {}).get("downloaded")
            and asset_is_valid(args.workspace, asset)
        }
    manifest = acquire_visuals(
        file_key=file_key,
        design_version=raw.get("version"),
        workspace=args.workspace,
        client=FigmaClient(token),
        include_originals=args.include_originals,
        plan=reviewed,
        inventory_provenance={
            "inventory_manifest_sha256": sha256_file(args.workspace / "manifest.json"),
            "raw_file_sha256": sha256_file(args.workspace / "raw" / "file.json"),
            "plan_sha256": sha256_file(plan_path),
        },
        roles=roles,
        unit_ids=unit_ids,
        render_batch_size=2 if args.role == ["overview"] else 50,
    )
    print(json.dumps({
        **_acquisition_result(
            args.workspace,
            reviewed,
            manifest,
            selected_ids,
            reusable_before,
        ),
        "requested_roles": args.role or ["overview", "detail"],
        "requested_units": args.unit_id or "all",
    }, sort_keys=True))


def _observe(args: argparse.Namespace) -> None:
    _require_valid_workspace(args.workspace)
    _require_visual_binding(args.workspace)
    value = record_observation(args.workspace, args.unit_id, {
        "layout": args.layout,
        "visual_style": args.visual_style,
        "states": args.state,
        "interactions": args.interaction,
        "uncertainties": args.uncertainty,
    })
    print(json.dumps(value, sort_keys=True))


def _status(args: argparse.Namespace) -> None:
    value = workspace_status(args.workspace)
    print(json.dumps(value, indent=2, sort_keys=True) if args.json else render_status(value))


def _handoff(args: argparse.Namespace) -> None:
    try:
        json_path, markdown_path, value = write_frontend_handoff(
            args.workspace,
            args.output_dir,
            unit_ids=set(args.unit_id) if args.unit_id else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(markdown_path),
        "unresolved_threads": len(value["unresolved_threads"]),
        "inspected_visual_units": value["coverage"]["inspected_visual_units"],
    }, sort_keys=True))


COMMANDS = {
    "doctor": _doctor,
    "demo": _demo,
    "ingest": _ingest,
    "validate": _validate,
    "plan": _plan,
    "acquire": _acquire,
    "observe": _observe,
    "status": _status,
    "handoff": _handoff,
}


def main() -> None:
    args = _parser().parse_args()
    try:
        COMMANDS[args.command](args)
    except (json.JSONDecodeError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()


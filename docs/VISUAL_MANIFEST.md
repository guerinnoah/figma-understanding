# Visual manifest and plan schemas

`normalized.json` uses `figma-inventory/v1`, `visual-manifest.json` uses
`visual-assets/v1`, reviewed inputs use `visual-plan/v1`, observations use
`visual-observations/v1`, and frontend handoffs use `frontend-handoff/v1`.

- `source`: file key, recorded design version, inventory/raw/plan hashes, and separate pinning claims for renders and originals.
- `render-plan.json`: deterministic render/original units with roles, source IDs, visible coverage, ancestor context, and auditable comment links.
- `assets`: acquisition state, linked thread IDs, media type, hash, and dimensions.
- `summary`: counts derived from asset state.
- `observations.json`: per-unit inspection findings bound to the current asset SHA-256 and node ID.
- `frontend-handoff.json`: implementation properties, design-system evidence, copy, assets, valid observations, comment discussions, coverage, and limitations.

The `render-plan.json` artifact binds deterministic comment linkage to the inventory manifest,
normalized snapshot, and comment-extraction status. Future incompatible meaning
changes require another schema identifier.


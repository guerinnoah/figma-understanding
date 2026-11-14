# Visual manifest and plan schemas

`normalized.json` uses `figma-inventory/v1`, `visual-manifest.json` uses
`visual-assets/v1`, reviewed inputs use `visual-plan/v2`, observations use
`visual-observations/v1`, and frontend handoffs use `frontend-handoff/v2`.
Within observation records, `inferred_interactions` contains structured
`claim`/`uncertainty` pairs rather than unqualified strings.

- `source`: file key, recorded design version, inventory/raw/plan hashes, and separate pinning claims for renders and originals.
- `render-plan.json`: deterministic render/original units, section-aware flow
  graphs with explicit prototype-transition or diagram-order semantics,
  incomplete prototype links, unresolved connectors, source IDs, visible
  coverage, ancestor context, explicit flow scope node IDs, and auditable
  comment links.
- `assets`: acquisition state, linked thread IDs, media type, hash, and dimensions.
- `summary`: counts derived from asset state.
- `observations.json`: per-unit inspection findings bound to the current asset SHA-256 and node ID.
- `frontend-handoff.json`: implementation properties, ordered flows,
  design-system evidence, copy, assets, valid observations, unresolved and
  resolved comment threads, sticky notes, Dev Mode annotations with pinned
  properties, coverage, and limitations.

The `render-plan.json` artifact binds deterministic comment linkage to the inventory manifest,
normalized snapshot, and comment-extraction status. Future incompatible meaning
changes require another schema identifier.


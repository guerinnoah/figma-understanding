# Review a visual evidence plan

Read this reference after `plan` and before downloading visual evidence.

## Scope selectors

`--node-id`, `--page`, `--section`, and `--flow` are repeatable. Pages and
sections accept an exact source ID or case-insensitive exact name. Flows accept
an exact flow ID, exact name, or a starting node ID when that start identifies
only one flow. `--max-detail` limits detail-only units while retaining
overviews.

Review `selection` to confirm how names and flow selectors resolved. A node
selection may retain a covering ancestor render as well as an exact render.

## Visual units

- `overview` units provide navigation and grouping context.
- `detail` units preserve evidence that requires closer inspection.
- One unit may satisfy both roles.
- `coverage_node_ids` lists the source nodes visible through that render.
- `context_path` gives page and ancestor names for otherwise generic units.
- `adaptive-tile` marks a direct child used to break up an oversized overview.
- `sparse-content` is a review warning, not proof that a render is useless.

Original image fills are excluded unless planning uses `--include-originals`.
They may contain pixels outside the visible crop and are not historically
pinned. Acquisition must use the matching opt-in.

## Ordered evidence

Each flow includes a stable ID, source kind, semantic boundary, steps, edges,
starts, sections, branches, joins, cycles, and `scope_node_ids` containing its
participating screens and evidence nodes.

- `prototype` flows are explicit prototype-transition evidence; action types may
  represent navigation, overlays, scrolling, or component changes.
- Prototype edges preserve navigation mode, trigger type, and whether a
  destination is conditional. They model topology, not trigger timing,
  animations, navigation options, or condition expressions.
- `connector` flows are explicit FigJam diagram order only.
- Flow records are derived from and validated against `raw/file.json`; change
  selectors and rerun `plan` instead of hand-editing graph evidence.
- `topological-layers` preserves dependency order in an acyclic graph.
- `shortest-path-from-sources` is used for cyclic graphs and is not a linear
  execution sequence.
- `complete: false` means a prototype start or destination is missing from the
  captured file.

Every edge cites its source, destination, and evidence node. Do not infer order
from canvas position or from connectors listed in `unresolved_connectors`.
Unresolved connectors have an unattached or missing endpoint, no directional
arrowhead, or arrowheads at both ends.

For spatially inferred order, repeat `--interaction` and `--uncertainty` in
matching order. The tool stores each claim and caveat as one structured
inference and rejects unpaired interaction claims.

## Comments

`comment_links` records one outcome for every captured thread. A thread links
to an exact render when possible, otherwise to the smallest render covering its
node. Invalid, stale, unanchored, or uncovered threads remain explicitly
unlinked. Resolved comments are discussion history, not automatically approved
requirements.

## Acquisition review

Before running `acquire`, confirm:

- the selected pages, sections, nodes, or flows match the request;
- the first acquisition is limited to overviews on a large board;
- requested detail units are relevant to the current question;
- original-image access was explicitly requested when enabled;
- incomplete flows and unresolved connectors will be reported as limitations.

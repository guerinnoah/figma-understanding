from __future__ import annotations

from typing import Any, Iterable

ARROW_CAPS = {"LINE_ARROW", "TRIANGLE_ARROW", "TRIANGLE_FILLED"}
NAVIGATION_TYPES = {"NAVIGATE", "OVERLAY", "SWAP", "SCROLL_TO", "CHANGE_TO"}


def _node_map(normalized_nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(node["id"]): node
        for node in normalized_nodes
        if isinstance(node, dict) and node.get("id")
    }


def _children_by_parent(nodes: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        parent_id = node.get("parent_id")
        if isinstance(parent_id, str):
            children.setdefault(parent_id, []).append(node_id)
    for values in children.values():
        values.sort()
    return children


def _descendant_ids(
    node_id: str,
    children_by_parent: dict[str, list[str]],
) -> Iterable[str]:
    pending = [node_id]
    while pending:
        descendant_id = pending.pop()
        yield descendant_id
        pending.extend(reversed(children_by_parent.get(descendant_id, [])))


def _section_context(normalized_nodes: Iterable[dict[str, Any]]) -> dict[str, str]:
    nodes = _node_map(normalized_nodes)
    section_by_node: dict[str, str] = {}
    for node_id, node in nodes.items():
        current_id: str | None = node_id
        seen = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            current = nodes.get(current_id)
            if current is None:
                break
            if current.get("type") == "SECTION":
                section_by_node[node_id] = current_id
                break
            parent_id = current.get("parent_id")
            current_id = parent_id if isinstance(parent_id, str) else None
    return section_by_node


def _interaction_edges(
    node_id: str,
    nodes: dict[str, dict[str, Any]],
    children_by_parent: dict[str, list[str]],
) -> list[dict[str, Any]]:
    if nodes.get(node_id, {}).get("visible") is False:
        return []
    edges = []
    seen = set()
    for descendant_id in _descendant_ids(node_id, children_by_parent):
        descendant = nodes.get(descendant_id, {})
        if descendant.get("visible") is False:
            continue
        prototype = descendant.get("implementation", {}).get("prototype", {})
        has_modern_topology = False
        for key in ("interactions", "reactions"):
            interactions = prototype.get(key)
            if not isinstance(interactions, list):
                continue
            for interaction in interactions:
                if not isinstance(interaction, dict):
                    continue
                actions = interaction.get("actions")
                if not isinstance(actions, list):
                    actions = [interaction.get("action")]
                for action, navigation, conditional in _node_actions(actions):
                    destination_id = str(action["destinationId"])
                    destination = nodes.get(destination_id)
                    if destination is not None and destination.get("visible") is False:
                        continue
                    has_modern_topology = True
                    edge = {
                        "kind": "prototype",
                        "source_node_id": node_id,
                        "destination_node_id": destination_id,
                        "evidence_node_id": descendant_id,
                        "action": navigation,
                    }
                    trigger = interaction.get("trigger")
                    if isinstance(trigger, dict) and trigger.get("type"):
                        edge["trigger"] = str(trigger["type"])
                    if conditional:
                        edge["conditional"] = True
                    identity = tuple(edge.get(key) for key in (
                        "source_node_id",
                        "destination_node_id",
                        "evidence_node_id",
                        "trigger",
                        "action",
                        "conditional",
                    ))
                    if identity not in seen:
                        seen.add(identity)
                        edges.append(edge)
        if not has_modern_topology and prototype.get("transition_node_id"):
            destination_id = str(prototype["transition_node_id"])
            destination = nodes.get(destination_id)
            if destination is not None and destination.get("visible") is False:
                continue
            edge = {
                "kind": "prototype",
                "source_node_id": node_id,
                "destination_node_id": destination_id,
                "evidence_node_id": descendant_id,
                "action": "TRANSITION",
            }
            identity = tuple(edge.get(key) for key in (
                "source_node_id", "destination_node_id", "evidence_node_id", "trigger", "action"
            ))
            if identity not in seen:
                seen.add(identity)
                edges.append(edge)
    return edges


def _node_actions(
    actions: Any,
    *,
    conditional: bool = False,
) -> Iterable[tuple[dict[str, Any], str, bool]]:
    """Yield topology-relevant node actions without interpreting their conditions."""
    if not isinstance(actions, list):
        return
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "CONDITIONAL":
            blocks = action.get("conditionalBlocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if isinstance(block, dict):
                    yield from _node_actions(block.get("actions"), conditional=True)
            continue
        if not action.get("destinationId"):
            continue
        if action_type == "NODE":
            navigation = action.get("navigation")
            navigation = (
                navigation
                if isinstance(navigation, str) and navigation in NAVIGATION_TYPES
                else "NODE"
            )
        elif isinstance(action_type, str) and action_type in NAVIGATION_TYPES:
            navigation = str(action_type)
        elif action_type is None:
            navigation = "NODE"
        else:
            continue
        yield action, navigation, conditional


def _graph_maps(
    node_ids: set[str],
    edges: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    adjacency = {node_id: set() for node_id in node_ids}
    reverse = {node_id: set() for node_id in node_ids}
    for edge in edges:
        source = edge["source_node_id"]
        destination = edge["destination_node_id"]
        if source in node_ids and destination in node_ids:
            adjacency[source].add(destination)
            reverse[destination].add(source)
    return adjacency, reverse


def _strong_components(
    node_ids: set[str],
    adjacency: dict[str, set[str]],
    reverse: dict[str, set[str]],
) -> list[list[str]]:
    seen = set()
    finish_order = []
    for start in sorted(node_ids):
        if start in seen:
            continue
        stack = [(start, False)]
        while stack:
            node_id, expanded = stack.pop()
            if expanded:
                finish_order.append(node_id)
                continue
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.append((node_id, True))
            stack.extend(
                (destination, False)
                for destination in reversed(sorted(adjacency[node_id]))
                if destination not in seen
            )

    assigned = set()
    components = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component = []
        pending = [start]
        assigned.add(start)
        while pending:
            node_id = pending.pop()
            component.append(node_id)
            for source in sorted(reverse[node_id], reverse=True):
                if source not in assigned:
                    assigned.add(source)
                    pending.append(source)
        components.append(sorted(component))
    return sorted(components, key=lambda component: component[0])


def _flow_layers(
    node_ids: set[str],
    edges: list[dict[str, Any]],
    preferred_starts: list[str],
) -> tuple[dict[str, int], list[str], bool, str]:
    if not node_ids:
        return {}, [], False, "unresolved"
    adjacency, reverse = _graph_maps(node_ids, edges)
    components = _strong_components(node_ids, adjacency, reverse)
    component_by_node = {
        node_id: index
        for index, component in enumerate(components)
        for node_id in component
    }
    component_edges = {index: set() for index in range(len(components))}
    component_incoming = {index: set() for index in range(len(components))}
    for source, destinations in adjacency.items():
        source_component = component_by_node[source]
        for destination in destinations:
            destination_component = component_by_node[destination]
            if source_component != destination_component:
                component_edges[source_component].add(destination_component)
                component_incoming[destination_component].add(source_component)

    source_components = sorted(
        (index for index, incoming in component_incoming.items() if not incoming),
        key=lambda index: components[index][0],
    )
    starts = []
    for index in source_components:
        preferred = sorted(node_id for node_id in preferred_starts if node_id in components[index])
        starts.append(preferred[0] if preferred else components[index][0])

    cyclic = any(len(component) > 1 for component in components) or any(
        node_id in adjacency[node_id] for node_id in node_ids
    )
    if cyclic:
        distances: dict[str, int] = {}
        pending = [(node_id, 0) for node_id in starts]
        while pending:
            node_id, distance = pending.pop(0)
            if node_id in distances and distances[node_id] <= distance:
                continue
            distances[node_id] = distance
            pending.extend(
                (destination, distance + 1)
                for destination in sorted(adjacency[node_id])
            )
        return distances, starts, True, "shortest-path-from-sources"

    component_distances = {index: 0 for index in source_components}
    incoming_counts = {
        index: len(incoming)
        for index, incoming in component_incoming.items()
    }
    pending_components = list(source_components)
    while pending_components:
        source = pending_components.pop(0)
        for destination in sorted(component_edges[source], key=lambda index: components[index][0]):
            component_distances[destination] = max(
                component_distances.get(destination, 0),
                component_distances[source] + 1,
            )
            incoming_counts[destination] -= 1
            if incoming_counts[destination] == 0:
                pending_components.append(destination)
    distances = {
        node_id: component_distances[component_by_node[node_id]]
        for node_id in node_ids
    }
    return distances, starts, False, "topological-layers"


def _flow_record(
    *,
    flow_id: str,
    name: str,
    kind: str,
    page_id: str,
    start_node_ids: list[str],
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    section_by_node: dict[str, str],
) -> dict[str, Any]:
    unresolved_start_node_ids = sorted(
        node_id for node_id in start_node_ids if node_id not in nodes
    )
    valid_edges = []
    unresolved_edges = []
    for edge in edges:
        missing = [
            role
            for role, node_id in (
                ("source", edge["source_node_id"]),
                ("destination", edge["destination_node_id"]),
            )
            if node_id not in nodes
        ]
        if missing:
            unresolved_edges.append({
                **edge,
                "reason": "-and-".join(missing) + "-node-missing",
            })
        else:
            valid_edges.append(edge)
    node_ids = {
        node_id
        for edge in valid_edges
        for node_id in (edge["source_node_id"], edge["destination_node_id"])
        if node_id in nodes
    } | {node_id for node_id in start_node_ids if node_id in nodes}
    adjacency, incoming = _graph_maps(node_ids, valid_edges)
    distances, effective_starts, cyclic, ordering = _flow_layers(
        node_ids,
        valid_edges,
        start_node_ids,
    )
    for edge in valid_edges:
        source = edge["source_node_id"]
        destination = edge["destination_node_id"]
        incoming[destination].add(source)

    steps = []
    for order in sorted(set(distances.values())):
        step_nodes = []
        for node_id in sorted(node_id for node_id, value in distances.items() if value == order):
            node = nodes[node_id]
            step_node = {
                "node_id": node_id,
                "name": str(node.get("name", "")),
                "type": str(node.get("type", "UNKNOWN")),
            }
            if node_id in section_by_node:
                step_node["section_id"] = section_by_node[node_id]
            step_nodes.append(step_node)
        steps.append({"order": order, "nodes": step_nodes})

    ordered_edges = sorted(
        valid_edges,
        key=lambda edge: (
            distances.get(edge["source_node_id"], 0),
            edge["source_node_id"],
            edge["destination_node_id"],
            edge["evidence_node_id"],
        ),
    )
    evidence_node_ids = {
        str(edge["evidence_node_id"])
        for edge in valid_edges + unresolved_edges
        if edge.get("evidence_node_id") in nodes
    }
    participant_node_ids = node_ids | evidence_node_ids
    section_ids = sorted({
        section_by_node[node_id]
        for node_id in participant_node_ids
        if node_id in section_by_node
    })
    scope_node_ids = sorted(node_ids | set(section_ids) | evidence_node_ids)
    return {
        "flow_id": flow_id,
        "name": name,
        "kind": kind,
        "semantics": "prototype-transition" if kind == "prototype" else "diagram-order",
        "page_id": page_id,
        "sections": [{
            "node_id": section_id,
            "name": str(nodes.get(section_id, {}).get("name", "")),
        } for section_id in section_ids],
        "start_node_ids": effective_starts,
        "unresolved_start_node_ids": unresolved_start_node_ids,
        "ordering": ordering,
        "steps": steps,
        "edges": ordered_edges,
        "unresolved_edges": sorted(
            unresolved_edges,
            key=lambda edge: (
                edge["source_node_id"],
                edge["destination_node_id"],
                edge["evidence_node_id"],
            ),
        ),
        "branch_node_ids": sorted(node_id for node_id, values in adjacency.items() if len(values) > 1),
        "join_node_ids": sorted(node_id for node_id, values in incoming.items() if len(values) > 1),
        "scope_node_ids": scope_node_ids,
        "cyclic": cyclic,
        "complete": not unresolved_start_node_ids and not unresolved_edges,
    }


def _prototype_flows(
    normalized_nodes: Iterable[dict[str, Any]],
    section_by_node: dict[str, str],
) -> list[dict[str, Any]]:
    nodes = _node_map(normalized_nodes)
    children_by_parent = _children_by_parent(nodes)
    flows = []
    for page_id, page in sorted(nodes.items()):
        if page.get("type") != "CANVAS":
            continue
        prototype = page.get("implementation", {}).get("prototype", {})
        starting_points = prototype.get("flow_starting_points", []) or []
        if not starting_points and prototype.get("prototype_start_node_id"):
            starting_points = [{"nodeId": prototype["prototype_start_node_id"]}]
        for flow in starting_points:
            if isinstance(flow, dict) and flow.get("nodeId"):
                start_id = str(flow["nodeId"])
                if nodes.get(start_id, {}).get("visible") is False:
                    continue
                pending = [start_id]
                visited = set()
                edges = []
                edge_ids = set()
                while pending:
                    node_id = pending.pop(0)
                    if node_id in visited:
                        continue
                    visited.add(node_id)
                    for edge in _interaction_edges(node_id, nodes, children_by_parent):
                        identity = (
                            edge["source_node_id"],
                            edge["destination_node_id"],
                            edge["evidence_node_id"],
                            edge.get("trigger"),
                            edge.get("action"),
                            edge.get("conditional"),
                        )
                        if identity not in edge_ids:
                            edge_ids.add(identity)
                            edges.append(edge)
                        if edge["destination_node_id"] in nodes:
                            pending.append(edge["destination_node_id"])
                flows.append(_flow_record(
                    flow_id=f"prototype:{page_id}:{start_id}",
                    name=str(flow.get("name", "")) or "Unnamed prototype flow",
                    kind="prototype",
                    page_id=page_id,
                    start_node_ids=[start_id],
                    edges=edges,
                    nodes=nodes,
                    section_by_node=section_by_node,
                ))
    return sorted(flows, key=lambda flow: (flow["name"].casefold(), flow["flow_id"]))


def _selected_flows(
    flows: list[dict[str, Any]],
    flow_names: set[str],
) -> list[dict[str, Any]]:
    selected = []
    missing = set()
    ambiguous = set()
    for selector in sorted(flow_names):
        by_flow_id = [flow for flow in flows if selector == flow["flow_id"]]
        by_start_id = [flow for flow in flows if selector in flow["start_node_ids"]]
        matches = by_flow_id or by_start_id or [
            flow
            for flow in flows
            if flow["name"].casefold() == selector.casefold()
        ]
        if not matches:
            missing.add(selector)
        elif len(matches) > 1:
            ambiguous.add(selector)
        else:
            selected.extend(matches)
    if missing:
        raise ValueError(f"unknown flow(s): {', '.join(sorted(missing))}")
    if ambiguous:
        raise ValueError(f"ambiguous flow name(s): {', '.join(sorted(ambiguous))}")
    return sorted(
        {flow["flow_id"]: flow for flow in selected}.values(),
        key=lambda flow: flow["flow_id"],
    )


def _connector_flows(
    normalized_nodes: Iterable[dict[str, Any]],
    section_by_node: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = _node_map(normalized_nodes)

    edges = []
    unresolved = []
    for connector in (
        node
        for node in nodes.values()
        if node.get("type") == "CONNECTOR" and node.get("visible") is not False
    ):
        connector_id = str(connector.get("id", ""))
        connector_evidence = connector.get("implementation", {}).get("connector", {})
        start = connector_evidence.get("start")
        end = connector_evidence.get("end")
        start_id = (
            str(start.get("endpointNodeId") or start.get("endpoint_node_id"))
            if isinstance(start, dict) and (start.get("endpointNodeId") or start.get("endpoint_node_id"))
            else None
        )
        end_id = (
            str(end.get("endpointNodeId") or end.get("endpoint_node_id"))
            if isinstance(end, dict) and (end.get("endpointNodeId") or end.get("endpoint_node_id"))
            else None
        )
        start_cap = str(connector_evidence.get("start_cap", "NONE"))
        end_cap = str(connector_evidence.get("end_cap", "NONE"))
        evidence = {
            "connector_node_id": connector_id,
            "start_node_id": start_id,
            "end_node_id": end_id,
            "start_cap": start_cap,
            "end_cap": end_cap,
            "page_id": str(connector.get("page_id") or ""),
            "section_id": section_by_node.get(connector_id),
        }
        if not start_id or not end_id:
            unresolved.append({**evidence, "reason": "unattached-endpoint"})
            continue
        if start_id not in nodes or end_id not in nodes:
            unresolved.append({**evidence, "reason": "endpoint-node-missing"})
            continue
        if (
            nodes[start_id].get("visible") is False
            or nodes[end_id].get("visible") is False
        ):
            continue
        start_arrow = start_cap in ARROW_CAPS
        end_arrow = end_cap in ARROW_CAPS
        if start_arrow == end_arrow:
            reason = "bidirectional-arrowheads" if start_arrow else "no-directional-arrowhead"
            unresolved.append({**evidence, "reason": reason})
            continue
        source_id, destination_id = (start_id, end_id) if end_arrow else (end_id, start_id)
        edge = {
            "kind": "connector",
            "source_node_id": source_id,
            "destination_node_id": destination_id,
            "evidence_node_id": connector_id,
        }
        line_type = connector_evidence.get("line_type")
        if line_type is not None:
            edge["line_type"] = str(line_type)
        edges.append(edge)

    undirected: dict[str, set[str]] = {}
    for edge in edges:
        source = edge["source_node_id"]
        destination = edge["destination_node_id"]
        undirected.setdefault(source, set()).add(destination)
        undirected.setdefault(destination, set()).add(source)
    flows = []
    seen = set()
    for first in sorted(undirected):
        if first in seen:
            continue
        pending = [first]
        component = set()
        while pending:
            node_id = pending.pop(0)
            if node_id in component:
                continue
            component.add(node_id)
            pending.extend(sorted(undirected.get(node_id, set()) - component))
        seen.update(component)
        component_edges = [
            edge for edge in edges
            if edge["source_node_id"] in component and edge["destination_node_id"] in component
        ]
        _, starts, _, _ = _flow_layers(component, component_edges, [])
        start_names = [str(nodes.get(node_id, {}).get("name", "")) for node_id in starts]
        name = (
            f"{start_names[0]} diagram"
            if len(starts) == 1 and start_names[0]
            else "Connector diagram"
        )
        evidence_id = min(edge["evidence_node_id"] for edge in component_edges)
        page_id = str(nodes.get(evidence_id, {}).get("page_id") or "")
        flows.append(_flow_record(
            flow_id=f"connector:{evidence_id}",
            name=name,
            kind="connector",
            page_id=page_id,
            start_node_ids=starts,
            edges=component_edges,
            nodes=nodes,
            section_by_node=section_by_node,
        ))
    return sorted(flows, key=lambda flow: flow["flow_id"]), sorted(
        unresolved,
        key=lambda item: item["connector_node_id"],
    )


def build_flow_evidence(
    normalized_nodes: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build all explicit ordered evidence from canonical normalized nodes."""
    normalized_nodes = list(normalized_nodes)
    section_by_node = _section_context(normalized_nodes)
    prototype_flows = _prototype_flows(normalized_nodes, section_by_node)
    connector_flows, unresolved_connectors = _connector_flows(
        normalized_nodes,
        section_by_node,
    )
    return prototype_flows + connector_flows, unresolved_connectors


def _flow_node_ids(flow: dict[str, Any]) -> list[str]:
    return [node["node_id"] for step in flow["steps"] for node in step["nodes"]]


def _selected_sections(
    nodes: dict[str, dict[str, Any]],
    section_names: set[str],
) -> list[str]:
    sections: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        if node.get("type") == "SECTION":
            sections.setdefault(str(node.get("name", "")).casefold(), []).append(node_id)
    selected = []
    missing = set()
    ambiguous = set()
    for name in sorted(section_names):
        node = nodes.get(name)
        matches = [name] if node and node.get("type") == "SECTION" else sorted(
            sections.get(name.casefold(), [])
        )
        if not matches:
            missing.add(name)
        elif len(matches) > 1:
            ambiguous.add(name)
        else:
            selected.append(matches[0])
    if missing:
        raise ValueError(f"unknown section(s): {', '.join(sorted(missing))}")
    if ambiguous:
        raise ValueError(f"ambiguous section name(s): {', '.join(sorted(ambiguous))}")
    return selected


def _selected_pages(
    nodes: dict[str, dict[str, Any]],
    page_names: set[str],
) -> list[str]:
    pages: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        if node.get("type") == "CANVAS":
            pages.setdefault(str(node.get("name", "")).casefold(), []).append(node_id)
    selected = []
    missing = set()
    ambiguous = set()
    for name in sorted(page_names):
        node = nodes.get(name)
        matches = [name] if node and node.get("type") == "CANVAS" else sorted(
            pages.get(name.casefold(), [])
        )
        if not matches:
            missing.add(name)
        elif len(matches) > 1:
            ambiguous.add(name)
        else:
            selected.append(matches[0])
    if missing:
        raise ValueError(f"unknown page(s): {', '.join(sorted(missing))}")
    if ambiguous:
        raise ValueError(f"ambiguous page name(s): {', '.join(sorted(ambiguous))}")
    return selected


def flow_record_errors(
    flows: Any,
    nodes: dict[str, dict[str, Any]],
    page_ids: set[str],
) -> list[str]:
    """Validate serialized flow records against normalized node evidence."""
    if not isinstance(flows, list):
        return ["render plan flows must be an array"]
    errors = []
    flow_ids = set()
    for index, flow in enumerate(flows):
        label = f"flow {index}"
        if not isinstance(flow, dict):
            errors.append(f"{label} must be an object")
            continue
        flow_id = flow.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id or flow_id in flow_ids:
            errors.append(f"{label} has invalid or duplicate flow_id")
        else:
            flow_ids.add(flow_id)
        kind = flow.get("kind")
        if not isinstance(kind, str) or kind not in {"prototype", "connector"}:
            errors.append(f"{label} has invalid kind")
        expected_semantics = (
            "prototype-transition" if kind == "prototype"
            else "diagram-order" if kind == "connector"
            else None
        )
        if flow.get("semantics") != expected_semantics:
            errors.append(f"{label} has invalid semantics")
        if not isinstance(flow.get("name"), str):
            errors.append(f"{label} has invalid name")
        page_id = flow.get("page_id")
        if not isinstance(page_id, str) or page_id not in page_ids:
            errors.append(f"{label} has invalid page_id")
        ordering = flow.get("ordering")
        if not isinstance(ordering, str) or ordering not in {
            "topological-layers",
            "shortest-path-from-sources",
            "unresolved",
        }:
            errors.append(f"{label} has invalid ordering")

        sections = flow.get("sections")
        section_ids = set()
        if not isinstance(sections, list) or any(
            not isinstance(section, dict)
            or not isinstance(section.get("node_id"), str)
            or nodes.get(section["node_id"], {}).get("type") != "SECTION"
            or not isinstance(section.get("name"), str)
            for section in sections or []
        ):
            errors.append(f"{label} has invalid sections")
        else:
            section_ids = {section["node_id"] for section in sections}

        steps = flow.get("steps")
        step_node_ids = set()
        if not isinstance(steps, list):
            errors.append(f"{label} steps must be an array")
            steps = []
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("order") != step_index:
                errors.append(f"{label} step {step_index} has invalid order")
                continue
            step_nodes = step.get("nodes")
            if not isinstance(step_nodes, list) or not step_nodes:
                errors.append(f"{label} step {step_index} has invalid nodes")
                continue
            for step_node in step_nodes:
                node_id = step_node.get("node_id") if isinstance(step_node, dict) else None
                if (
                    not isinstance(step_node, dict)
                    or not isinstance(node_id, str)
                    or node_id not in nodes
                    or node_id in step_node_ids
                    or not isinstance(step_node.get("name"), str)
                    or not isinstance(step_node.get("type"), str)
                ):
                    errors.append(f"{label} step {step_index} has invalid node evidence")
                    continue
                step_node_ids.add(node_id)
                section_id = step_node.get("section_id")
                if section_id is not None and (
                    not isinstance(section_id, str)
                    or nodes.get(section_id, {}).get("type") != "SECTION"
                ):
                    errors.append(f"{label} step {step_index} has invalid section_id")

        starts = flow.get("start_node_ids")
        if not isinstance(starts, list) or any(
            not isinstance(node_id, str) or node_id not in step_node_ids
            for node_id in starts or []
        ):
            errors.append(f"{label} has invalid start_node_ids")
        unresolved_starts = flow.get("unresolved_start_node_ids")
        if not isinstance(unresolved_starts, list) or any(
            not isinstance(node_id, str) or node_id in nodes
            for node_id in unresolved_starts or []
        ):
            errors.append(f"{label} has invalid unresolved_start_node_ids")

        evidence_node_ids = set()
        edges = flow.get("edges")
        if not isinstance(edges, list):
            errors.append(f"{label} edges must be an array")
            edges = []
        for edge_index, edge in enumerate(edges):
            source_id = edge.get("source_node_id") if isinstance(edge, dict) else None
            destination_id = edge.get("destination_node_id") if isinstance(edge, dict) else None
            evidence_id = edge.get("evidence_node_id") if isinstance(edge, dict) else None
            if (
                not isinstance(edge, dict)
                or edge.get("kind") != kind
                or not isinstance(source_id, str)
                or source_id not in step_node_ids
                or not isinstance(destination_id, str)
                or destination_id not in step_node_ids
                or not isinstance(evidence_id, str)
                or evidence_id not in nodes
                or any(
                    key in edge and not isinstance(edge[key], str)
                    for key in ("trigger", "action", "line_type")
                )
                or (
                    "conditional" in edge
                    and not isinstance(edge["conditional"], bool)
                )
            ):
                errors.append(f"{label} edge {edge_index} has invalid node evidence")
            else:
                evidence_node_ids.add(evidence_id)

        unresolved_edges = flow.get("unresolved_edges")
        if not isinstance(unresolved_edges, list):
            errors.append(f"{label} unresolved_edges must be an array")
            unresolved_edges = []
        for edge_index, edge in enumerate(unresolved_edges):
            evidence_id = edge.get("evidence_node_id") if isinstance(edge, dict) else None
            reason = edge.get("reason") if isinstance(edge, dict) else None
            if (
                not isinstance(edge, dict)
                or edge.get("kind") != kind
                or not isinstance(evidence_id, str)
                or evidence_id not in nodes
                or not isinstance(edge.get("source_node_id"), str)
                or not isinstance(edge.get("destination_node_id"), str)
                or not isinstance(reason, str)
                or reason not in {
                    "source-node-missing",
                    "destination-node-missing",
                    "source-and-destination-node-missing",
                }
            ):
                errors.append(f"{label} unresolved edge {edge_index} is invalid")
            else:
                evidence_node_ids.add(evidence_id)

        for key in ("branch_node_ids", "join_node_ids"):
            values = flow.get(key)
            if not isinstance(values, list) or any(
                not isinstance(value, str) or value not in step_node_ids
                for value in values or []
            ):
                errors.append(f"{label} has invalid {key}")
        scope_node_ids = flow.get("scope_node_ids")
        expected_scope = sorted(step_node_ids | section_ids | evidence_node_ids)
        if not isinstance(scope_node_ids, list) or scope_node_ids != expected_scope:
            errors.append(f"{label} has invalid scope_node_ids")
        if not isinstance(flow.get("cyclic"), bool):
            errors.append(f"{label} has invalid cyclic flag")
        complete = flow.get("complete")
        expected_complete = not unresolved_starts and not unresolved_edges
        if not isinstance(complete, bool) or complete != expected_complete:
            errors.append(f"{label} has inconsistent complete flag")
    return errors


def unresolved_connector_errors(
    connectors: Any,
    nodes: dict[str, dict[str, Any]],
    page_ids: set[str],
) -> list[str]:
    """Validate unresolved connector evidence against normalized nodes."""
    if not isinstance(connectors, list):
        return ["render plan unresolved_connectors must be an array"]
    errors = []
    seen = set()
    reasons = {
        "unattached-endpoint",
        "endpoint-node-missing",
        "bidirectional-arrowheads",
        "no-directional-arrowhead",
    }
    for index, connector in enumerate(connectors):
        label = f"unresolved connector {index}"
        if not isinstance(connector, dict):
            errors.append(f"{label} must be an object")
            continue
        connector_id = connector.get("connector_node_id")
        if (
            not isinstance(connector_id, str)
            or nodes.get(connector_id, {}).get("type") != "CONNECTOR"
            or connector_id in seen
        ):
            errors.append(f"{label} has invalid connector_node_id")
        else:
            seen.add(connector_id)
        page_id = connector.get("page_id")
        if not isinstance(page_id, str) or page_id not in page_ids:
            errors.append(f"{label} has invalid page_id")
        reason = connector.get("reason")
        if not isinstance(reason, str) or reason not in reasons:
            errors.append(f"{label} has invalid reason")
        for key in ("start_node_id", "end_node_id", "section_id"):
            value = connector.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"{label} has invalid {key}")
        section_id = connector.get("section_id")
        if section_id is not None and (
            not isinstance(section_id, str)
            or nodes.get(section_id, {}).get("type") != "SECTION"
        ):
            errors.append(f"{label} has invalid section_id")
        for key in ("start_cap", "end_cap"):
            if not isinstance(connector.get(key), str):
                errors.append(f"{label} has invalid {key}")
    return errors


def canonical_flow_evidence_errors(
    flows: Any,
    unresolved_connectors: Any,
    expected_flows: list[dict[str, Any]],
    expected_unresolved_connectors: list[dict[str, Any]],
) -> list[str]:
    """Reject serialized evidence that differs from the immutable raw snapshot."""
    errors = []
    if isinstance(flows, list) and flows != expected_flows:
        errors.append("render plan flows do not match raw Figma evidence and selection")
    if (
        isinstance(unresolved_connectors, list)
        and unresolved_connectors != expected_unresolved_connectors
    ):
        errors.append(
            "render plan unresolved connectors do not match raw Figma evidence and selection"
        )
    return errors

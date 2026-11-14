# Synthetic checkout example

This example shows how Figma evidence can inform a frontend without turning
unknowns into requirements.

Generate the synthetic inventory and `frontend-handoff/v1` artifacts:

~~~sh
python3 scripts/figma_tool.py demo --workspace ./workspaces/example-demo
python3 scripts/figma_tool.py handoff --workspace ./workspaces/example-demo
~~~

[checkout.html](checkout.html) is a small, dependency-free illustration of how
an implementation may use the handoff. It preserves known dimensions, layout,
typography, copy, and the unresolved comment while leaving responsive and
interactive behavior and uninspected visuals unconfirmed. It is not generated
application code or part of the skill runtime.

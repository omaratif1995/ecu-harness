# Fix validation findings

Use when: the user asks to fix errors or warnings, or your change produced new ones.

1. `ecu_validate` with `scope: all` (page with `offset`). Group by `id`: one cause often produces many findings.
2. For each group: `ecu_get` the `path`, read the message and its `fix`, and `ecu_describe` the parameter involved.
3. Fix causes, not symptoms. Never delete a container only to make a finding disappear, and never set an arbitrary value to silence one. If the right value is a design decision (an identifier, a timing, a length), ask the user.
4. One `ecu_edit` per cause, then `ecu_validate` again: `resolved` should rise and `new` should stay empty.
5. Findings from the vendor tool (their `source` is not `structure`) are authoritative. If the tool's hint and this recipe disagree, follow the tool.

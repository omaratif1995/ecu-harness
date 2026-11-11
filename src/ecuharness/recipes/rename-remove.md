# Rename or remove safely

Use when: a container must be renamed or deleted.

Rename: `ecu_edit` op `rename`. Every reference to the container, and to anything inside it, is updated in the same step. Check the result with `ecu_change_diff`.

Remove:

1. `ecu_get` the container: `referenced_by` shows who uses it.
2. Decide with the user what each of those should do instead: refer to another container (`set`) or lose the reference (`unset`).
3. One `ecu_edit` call with those ops first and the `remove` last. Removal is refused while anything still refers to the container.
4. The project limits how many containers one change may remove. At the limit, stop and ask; do not split the removal over several changes.

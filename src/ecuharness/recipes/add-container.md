# Add a container (signal, PDU, NvM block, trouble code, task, ...)

Use when: something new must be created in a module.

1. Look at the siblings in one call: `ecu_find` with `kind` (for example ComSignal) and `show` set to the parameters that must be unique or must not overlap (positions, identifiers).
2. `ecu_describe` that kind: note every parameter marked `required`, the ranges and the allowed values.
3. One `ecu_edit` call with a goal:
   - `add` with parent, kind, name and `values`; or `clone` an existing sibling and override what differs (this also works when no definitions are loaded);
   - in the same call, `append` the new container's path to every list that must refer to it (for a Com signal: `ComIPduSignalRef` of its I-PDU).
4. Fix what the result lists under `new`, then `ecu_change_finish`.

If other modules must know the new element too, use the recipe trace-and-mirror. Names: follow the pattern of the siblings (prefix and casing).

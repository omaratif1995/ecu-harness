# Change parameter values

Use when: an existing value must change.

1. Confirm the current value in one call: `ecu_find` with `show`, or `ecu_get`. If it is not what the user believes, say so before changing anything.
2. If unsure about type, range or allowed values: `ecu_describe` the kind with `filter` set to the parameter name.
3. `ecu_edit` with a goal: `set` for a single value. For list parameters `append` adds one value and `unset` with `value` removes one. `unset` without value clears the parameter, so its default applies.
4. Same change on many containers: collect the paths with `ecu_find` (`kind`, `param`, `value`), then send all `set` ops in one `ecu_edit` call (up to 50 ops per call).
5. A changed value often matters to other modules; the findings under `new` show where. Then `ecu_change_finish`.

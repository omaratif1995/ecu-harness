# Bring in a new customer input file (DBC, system description, ECU extract)

Use when: a new version of a DBC or ARXML input file arrived and the project must follow it.

1. `ecu_compare` with `new`, and `old` unless the earlier version is the last commit of the same path. Read all changes; page with `offset`.
2. Report the impact before changing anything: each change, and the `in_project` containers that carry its name. Say which changes have no counterpart in the project. `in_project` is a name match, not proof: read the containers with `ecu_get`.
3. In a vendor tool project the configuration is derived from the input files by the tool. If `ecu_script_run` exists and lists a task that updates the input files, run it with a `goal`. Otherwise ask the user to run the update in the vendor tool. Do not re-create derived containers by hand.
4. After the update: `ecu_change_diff` shows what the tool changed. Compare it with step 2. `ecu_validate` shows what is open now.
5. Fix what is left with `ecu_edit` (recipe fix-findings). Values the input does not define, such as timeouts, init values and mappings to the application, are decisions: ask.
6. `ecu_change_finish`. Report the input changes, what the tool derived, what you changed, every check, and what still needs the user.

Without a vendor tool: after the user confirmed the list from step 2, apply each change with `ecu_edit`; for new elements use recipe trace-and-mirror.

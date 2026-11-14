# Report the result

Use when: a task is done or stopped and the user wants the complete account.

Answer with these parts, short, in this order. Leave a part out only when it is empty.

1. Request: one sentence, as you understood it.
2. Project facts and their source: `autosar_schema` and `driver` from `ecu_overview`; a module's version from `ecu_describe` of that module.
3. Changed: the `changes` of `ecu_change_finish`, or "proposal only, nothing written".
4. Checks: the `checks` sentence of `ecu_change_finish`: what passed, what failed, what was not run. If the vendor tool did not validate, say so in those words.
5. Merge: each conflict and who decided it, if a merge was part of the task.
6. Open: decisions the user must take, values you assumed, and steps outside these tools (code, generation, build, tests).
7. Review and undo: the report path; `ecuh change undo <id>` restores the files; committing is the user's step.

Do not write "successful" or "valid" about anything that is not listed in `checks` as pass.

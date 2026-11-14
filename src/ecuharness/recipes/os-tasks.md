# Review OS tasks and runnable mapping

Use when: the user asks about task design, priorities, stack sizes, runnable mapping or CPU load.

Collect the facts first, in few calls:

1. Tasks: `ecu_describe` OsTask shows which scheduling parameters this project has. Then `ecu_find` with `kind` OsTask and `show` set to them (priority, schedule, activation, stack size, core).
2. Activation: `ecu_find` with `users_of` a task shows the alarms, schedule tables and mappings that refer to it.
3. Runnables per task: in the Rte module, `ecu_find` with the event-to-task mapping kind and `count_by` set to its task reference. Then list one task's mappings with `show` for position and offset.

Then reason, and mark every statement as configured, measured or estimated:

- Periods, offsets and priorities are configured facts. Execution times, stack use and CPU load are not in the configuration. Ask for measurements (stack high-water marks, runtime traces), or call your numbers estimates.
- Never derive a stack size from a task's name or from the number of its runnables.
- Regrouping runnables or changing priorities and preemption changes timing. Present options with their trade-offs. Do not apply one unasked.

Change scheduling only after the user chose an option: `ecu_edit`, then `ecu_change_finish`. A project can require a person's approval for these paths.

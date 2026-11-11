# The work loop

Use when: starting any task on the ECU configuration.

1. Look first. `ecu_find` locates things; add `show` to get parameter values in the same call, and `targets_of` / `users_of` to follow references. `ecu_get` reads elements (several paths in one call). `ecu_describe` tells what a kind or parameter allows.
2. Change with one `ecu_edit` call: a `goal` (one sentence, plus `ticket` if the user gave an id) and all related ops. It opens the change, checks every op, writes all or nothing and reports the findings your change introduced. If it returns an error, nothing was written: fix that op and resend all ops.
3. If `new` lists findings, fix them with another `ecu_edit`; each finding has a `fix`. Findings that existed before your change are not listed and are not yours to chase.
4. Close with `ecu_change_finish` and a summary. It validates again with the vendor tool if one is configured, lists what changed and writes the report. Tell the user what changed and where the report is.

`ecu_overview` shows the modules and whether a change is already open: continue an open change or ask the user. `ecu_change_diff` shows what the open change did so far.

If the goal cannot be reached validly after a few rounds, or needs something the tools refuse: `ecu_change_discard`, then tell the user what blocked you.

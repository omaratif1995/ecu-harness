# Plan a change that spans several modules

Use when: the request is a feature, not a value: a new CAN channel, a diagnostic identifier (DID), an Ethernet service, runnables mapped to tasks.

1. Find a working example of the same kind in this project and trace it (recipe trace-and-mirror): `ecu_find` with `users_of` and `targets_of`, module by module. The example shows which modules and containers are involved here. Do not assume the textbook set.
2. Before editing, give the user a plan: the containers you will add or change per module, the values taken from the request, the values copied from the example, and every value nobody gave you (identifiers, addresses, timing, sizes, session and security levels). Ask for those. Do not invent them.
3. Send the whole set to `ecu_edit` with `dry_run` first. Then apply it in the same change, lower layers first, and read `new` after each call.
4. `ecu_validate`. The vendor tool's findings name what is still missing.
5. `ecu_change_finish`. If it says a person must approve, stop and tell the user.

Not everything is configuration. Callbacks, application code, generated files and the build are outside these tools: list what the user still has to do there.

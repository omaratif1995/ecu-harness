# Review a change

Use when: the user asks what a change did, or wants a second look before committing.

1. `ecu_change_diff` for the open change. For a finished one, read `.ecuharness/changes/<id>/report.md`.
2. Compare every line with the stated goal. Anything the goal did not need is a finding: say so.
3. `ecu_validate`: read `gate`, `new` and `resolved`.
4. Report what changed, what validation says, and what you could not verify. Do not declare it correct; the vendor tool and the engineer decide that.

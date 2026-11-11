# When a tool refuses or you are stuck

Use when: a tool returned an error you cannot fix, or told you to ask the user.

- Read the `hint`. It names the fix, or tells you to stop.
- "read-only", "protected", "policy", "limit": these are decisions of the project owner. Stop and ask. Never try another route (file edits, scripts, another tool).
- "Files changed outside the harness": someone edited ARXML directly. Tell the user; they decide.
- "No definition is loaded": you can still read, `set` existing values and `clone`. New kinds need the definition files; tell the user which module is affected.
- Driver unavailable or failed: validation cannot be trusted and the change cannot be finished. Give the user the driver's message.
- Three failed attempts at the same step: stop, `ecu_change_discard` if a change is open, and explain what you tried.

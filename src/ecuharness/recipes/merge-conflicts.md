# Merge conflicts in ARXML files

Use when: git stopped a merge, rebase or cherry-pick on an `.arxml` file, or a file contains `<<<<<<<` markers.

Never edit the conflict markers, and never pick a whole file from one side.

1. `ecu_merge` without arguments. It merges each conflicted file by element: what only one side changed is kept from that side. It returns only the real conflicts, each with `base`, `ours` and `theirs`.
2. Show the conflicts to the user as a short table and ask which side each one takes. Decide yourself only what the user already told you. During a rebase, "ours" is the branch being rebased onto. A conflict marked `calculated` is a value the vendor tool computes on both sides: `keep_ours`, and tell the user that the tool's next update computes it again.
3. `ecu_merge` with the ids in `take_theirs` and `keep_ours`. For a value that is neither side: `keep_ours`, then set it with `ecu_edit`.
4. When no conflict is open, the result has `errors`. Two changes that merge cleanly can still contradict each other: a reference to something the other side deleted, two elements with the same identifier. Fix those with `ecu_edit`, and run `ecu_validate` when the result asks for it.
5. Tell the user: how many changes came from their side, each conflict with its decision, what validation says, and where the report is. Staging and committing are the user's steps.

Files listed under `not_merged` or `other_conflicted_files` are not yours to settle. Name them to the user.

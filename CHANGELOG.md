# Changes

## 0.3.0

New:

- **Merge by element.** `ecu_merge` and `ecuh merge` merge the ARXML files git left in conflict by matching containers and values instead of text lines. Only real conflicts remain, each with base, ours and theirs. `ecuh merge --install-driver` lets git do this during merge, rebase and cherry-pick. The result is proven by parsing it again; a merge report records what was taken and what was decided. Outside ECUC values the order of entries is merged like a value, and no decision ever drops content of either side.
- `ecu_overview` lists the `.arxml` files a git merge, rebase or cherry-pick has not settled, also when the merge driver left no conflict markers, and says when only staging is left.
- **Compare of input files.** `ecu_compare` shows what changed between two versions of an ARXML or DBC file, and which containers of the project carry the names of changed elements. Without `old` it compares with the last commit. For DBC files this includes message, signal, node and bus attributes (for example the baud rate), value tables and float signal types.
- **Approval by a person.** Paths listed in `policy.approvalPaths` can be changed but not finished until someone ran `ecuh change approve`.
- **Checks ledger and audit record.** `ecu_change_finish` returns one sentence saying which checks passed, failed or were not run. Each finished change has an `audit.json` with file hashes, findings, git branch and commit, user and approval.
- **Undo of a finished change:** `ecuh change undo <id>`.
- **Vendor tool commands as approved tasks.** `policy.scriptTasks` accepts `command` (words of the vendor command line, for example the update of the input files). `ecu_script_run` takes a `goal` and then runs inside a change, so what the tool rewrote can be reviewed and undone. A file the tool creates belongs to the change: the diff lists its content, and discard or undo move it into the change's folder.
- **Versions.** `ecu_overview` names the AUTOSAR schema of the project files and shows mixed versions; `ecu_describe` of a module shows the version facts found in its definition file.
- `ecu_find`: `count_by` counts matches per value; `users_of` and `targets_of` include what is inside the given container.
- Five procedures: merge-conflicts, integrate-input, plan-large-change, os-tasks, final-report.
- Three evaluation tasks: customer-input, merge-branches, needs-approval. The demo project has two versions of a CAN database.
- `tools.off` in `ecuharness.json` hides tools a project does not need. `ecu_merge` is offered only inside a git repository.

Changed:

- The tools every project gets grew from 5,839 to 6,593 characters (`ecu_compare` is new). `ecu_merge` adds 574 inside a git repository.
- The harness now starts `git` for reading (conflicted files, last commit, branch and commit). It still never stages or commits.
- `ecuh doctor` reports how merges are handled and fails when the merge driver of the clone points to a harness or Python that no longer exists.
- With a vendor tool driver, a change that opens right after another one finished, with no ARXML file changed in between, takes over that validation as its baseline instead of running the tool again. Finishing and `ecu_validate` always run the tool.
- `docs/TRIAL.md`: how to try the harness on a real project and measure the time saved per task.

Fixed:

- Discarding a change reported files as not restored when their names contained spaces.
- On Windows, `ecuh eval run` (and the tests) left temporary folders with a git repository behind, because git marks its files read-only.

## 0.2.0

First public version: reading, searching and verified editing of ECUC values over MCP and in the terminal, change sessions with gate, report and discard, drivers for `dvcfg-b` and `DvCliAdaptive` (experimental), eight procedures, eight evaluation tasks.

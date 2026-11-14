# ECU Harness

[![tests](https://github.com/omaratif1995/ecu-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/omaratif1995/ecu-harness/actions/workflows/tests.yml)

An AI agent harness for AUTOSAR ECU configuration.

It gives an AI assistant in VS Code or the GitHub Copilot app a small set of tools to read, change, merge and compare ECU configuration (ARXML) safely. Every edit is type-checked, written byte-exactly, proven by re-reading the result, validated against a baseline, and can be undone. The assistant never touches the XML itself.

The point of a harness is that the exact work is done by code, not by the model. A weak model gets reliable because it cannot make the mistakes the tools prevent. A strong model gets cheap because it does not spend tokens finding out how the task is done.

It works on Vector DaVinci Configurator Classic projects and on any other project that stores its ECU configuration as AUTOSAR ECUC ARXML.

> Independent open-source project. Not affiliated with or endorsed by Vector Informatik GmbH or the AUTOSAR partnership. DaVinci and MICROSAR are trademarks of Vector Informatik GmbH; AUTOSAR is a trademark of the AUTOSAR partnership. The repository contains no vendor files, definitions or documentation.

## What it covers

| Work of a configuration engineer | What the harness does | What stays with you or the vendor tool |
|---|---|---|
| Change the configuration from a request in plain language | Eleven tools (fourteen where all apply) and thirteen procedures. Edits are checked against the module definitions, written all-or-nothing and proven after writing | Values nobody gave: identifiers, timing, sizes. Callbacks and application code |
| Changes that span modules (a CAN channel, a DID, a service) | Follows the references of a working example across modules, plans, dry-runs, then edits in one change | Choosing the right example; what is not configuration |
| Two people changed the same ARXML file | Merges by element instead of by text line. Only real conflicts are left, each as one line: base, ours, theirs. Works after git stopped, or as git merge driver | Deciding the real conflicts |
| A new customer file arrives (DBC, system description, ECU extract) | Shows what changed, element by element, and which containers of the project carry those names. Runs the vendor tool's update as an approved task and records what it changed | Deriving configuration from input files is the vendor tool's job |
| OS tasks, runnable mapping, stack sizes | Gets the facts in few calls and keeps configured, measured and estimated apart. Scheduling paths can require a person's approval | Timing and stack measurements; the design decision |
| Knowing what a parameter allows | Takes it from the project's own module definitions, never from the model's memory | Validation against the AUTOSAR XSD is not built in |
| AUTOSAR and tool versions | Names the schema version of the project files, mixed versions, the schema difference of two compared files, and module versions from the definition files | Migration between releases |
| Traceability | Goal, ticket, journal of every operation, review report, `audit.json` with file hashes, checks, git branch and commit, approval | Linking it to your requirement system |
| Checks, approvals, rollback | A gate (no new errors), a ledger that says for every check pass, fail or not run, approval by a person for listed paths, discard and undo | Build and integration tests |

## Status: what is proven and what is not

| Part | Status |
|---|---|
| Read, search and edit ECUC values (containers, parameters, references) | Tested: 137 automated tests, byte-exact edits, two independent readers cross-checked |
| Change sessions: snapshot, baseline, gate, approval, report, audit record, undo | Tested |
| Merge by element, also as git merge driver | Tested with unit cases, with merge laws checked on random edits of both sides, and in real git repositories. Not yet tried on files written by a vendor tool |
| Compare of DBC and ARXML files | Tested on synthetic files |
| MCP server (protocol 2026-07-28, and 2025-11-25 and older) | Tested with a protocol-level client. Not yet confirmed by a person inside VS Code or the Copilot app |
| Driver for DaVinci Configurator Classic 6 (`dvcfg-b`) | Experimental. Written from Vector's public documentation and tested against a stand-in executable only. Confirm it with [docs/WORK-PC-CHECK.md](docs/WORK-PC-CHECK.md) |
| Driver for DaVinci Developer Adaptive (`DvCliAdaptive`) | Experimental, same caveat |
| Other ARXML: software components, system descriptions, Adaptive manifests | Can be searched, read, compared and merged. Cannot be edited |
| DaVinci Configurator Classic 5, DaVinci Developer Classic | Their ARXML files can be read, edited (ECUC), compared and merged at file level. No tool driver yet |
| Platforms | Developed on Windows with Python 3.10. The test workflow (badge above) runs the tests on Windows and Linux with Python 3.8, 3.11, 3.13 and 3.14 |

## Try it in two minutes

You need Python 3.8 or newer. Nothing is installed and nothing is downloaded besides this folder.

```
git clone https://github.com/omaratif1995/ecu-harness        # or download the ZIP and unpack it
python ecu-harness/ecuh.py demo my-ecu                        # a small synthetic ECU project
cd my-ecu
python ../ecu-harness/ecuh.py init                            # wires the folder to VS Code and the Copilot app
python ../ecu-harness/ecuh.py doctor                          # checks everything and starts the server once
```

Then:

1. **VS Code**: open the `my-ecu` folder, open Chat, choose Agent mode. Trust the `ecu-harness` server when VS Code asks. In the agent picker you can choose **ECU Engineer**: that agent is set up with read, search and the harness tools only, so it has no way to edit ARXML by hand.
2. **GitHub Copilot app / Copilot CLI**: open the folder as a project. The server is picked up from `.mcp.json` after you trust the folder.
3. Ask, for example:
   - "Which signals are in IPdu_EngineData and where do they sit?"
   - "Add an 8-bit signal Sig_OilPressure to IPdu_EngineData without overlapping the other signals."
   - "The customer sent Input/Vehicle_v2.dbc. What changed compared with Input/Vehicle.dbc, and what does it affect?"
   - "Rename PDU Pdu_EngineData to Pdu_Engine."

For a PC where only single files may be copied: `python tools/build_pyz.py` makes `dist/ecuh.pyz`, the whole harness in one file of about 125 kB that is used the same way (`python ecuh.pyz init`). A `pyproject.toml` is included for `pip install .`, which is not tested yet.

## Use it on your own project

```
cd <your project folder>
python <path>/ecuh.py init --bsw-package <path of your BSW package>
python <path>/ecuh.py doctor
```

`init` looks at the project and tells you what it found:

- the ECUC value files (they become editable) and other ARXML files;
- the module definition files in the BSW package. They enable type checks and adding new kinds of containers. Only the definitions of modules your project uses are loaded;
- a DaVinci Configurator project (`*.dvjson`) or DaVinci Developer Adaptive project (`*.adpa`), and the vendor command line if it is installed.

It writes `ecuharness.json` and the host files. Existing `.vscode/mcp.json` and `AGENTS.md` are merged into, never replaced. The MCP files contain paths of your PC, so each colleague runs `init` once.

`ecuharness.json` is short and can be edited by hand:

```json
{
  "name": "MyEcu",
  "model": {
    "config":      ["Config/ECUC/*.arxml"],
    "definitions": ["D:/BswPackage/**/*[bB][sS][wW][mM][dD]*.arxml"],
    "readonly":    ["Input/*.arxml"]
  },
  "driver": {
    "type": "dvcfg6",
    "exe": "C:/Program Files/Vector/DaVinci Configurator Classic 6/dvcfg-b.exe",
    "bswPackage": "D:/BswPackage"
  }
}
```

- **config**: files the harness may edit.
- **definitions**: module definition files (BSWMD). Without them the assistant can still read everything, change existing values and copy containers.
- **readonly**: other ARXML (ECU extract, system description, software components) for reading and for resolving references. Never edited.
- **driver**: who judges the configuration beyond the built-in structural checks: `none`, `dvcfg6`, `dvadaptive`, or `sim` (demo only).

All settings, including the policy, are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What the assistant gets

Eleven tools in every project, and three more where they can be used.

| Tool | Job |
|---|---|
| `ecu_overview` | Modules, AUTOSAR schema version, tool driver, open change, files left in conflict |
| `ecu_find` | Search by name, kind, parameter value or reference relation; `show` returns parameter values with the matches; `count_by` counts instead of listing |
| `ecu_get` | Read elements: values, references, children, who refers to them. Several paths in one call |
| `ecu_describe` | What a kind or parameter allows: type, range, allowed values, required. For a module: its version facts |
| `ecu_edit` | Operations: set, append, unset, add, clone, rename, remove. Opens the change, checks every op, reports new findings |
| `ecu_validate` | Full validation; findings this change introduced; whether it may be finished |
| `ecu_change_diff` | What the change modified, computed from the files |
| `ecu_change_finish` | Validate, close the change, write the review report and the audit record |
| `ecu_change_discard` | Undo the change |
| `ecu_merge` | Merge the ARXML files git left in conflict, by element; list and decide the real conflicts (only in a git repository) |
| `ecu_compare` | What changed between two versions of an ARXML or DBC file, and where those names occur in the project |
| `ecu_recipe` | Step-by-step procedures for common tasks |
| `ecu_generate` | Run the vendor code generator (only with a driver) |
| `ecu_script_run` | Run a vendor tool task the project owner approved (only if any is approved) |

The shortest path is three calls: look (`ecu_find`), change (`ecu_edit` with a goal), close (`ecu_change_finish`). You then read `.ecuharness/changes/<id>/report.md`, look at the diff in your version control, and commit yourself. The harness never commits.

A project that does not need a tool can switch it off and save its share of the context, for example `"tools": {"off": ["ecu_compare"]}` in `ecuharness.json`.

## Merging branches

Git merges text lines. ARXML is long, repetitive XML, so two engineers who add a signal each, or change two parameters of the same task, get a conflict that neither of them caused. The harness merges by element instead: a container is matched by its short name, a parameter value by its definition.

**After git stopped.** Ask the assistant to resolve the ARXML conflicts, or run `ecuh merge` yourself. The three versions git kept (common ancestor, ours, theirs) are merged, the result replaces the file with the conflict markers, and what is left is a list like this:

```
c1  /ActiveEcuC/Os/Task_10ms OsTaskPriority    base: 10 | ours: 11 | theirs: 12
```

You decide each line (`ecuh merge --theirs c1`, or tell the assistant), validate, and stage the files. The harness never stages or commits.

**During the merge.** `ecuh merge --install-driver` registers the same merge as a git merge driver for `*.arxml` in your clone. Git then calls it for merge, rebase and cherry-pick, and a file without a real conflict no longer stops the merge. The line it adds to `.gitattributes` is shared through the repository; the setting itself is per clone, so each colleague runs the command once.

| Situation | Result |
|---|---|
| Only one side changed an element | That side's version |
| Both sides made the same change | Kept once |
| Both sides added different elements to the same list | Both are kept |
| Both sides changed the same value differently | Conflict |
| One side deleted what the other changed | Conflict |
| Both sides added an element with the same name and different content | Conflict |
| One side changed the order of entries (outside ECUC values) | That side's order, with the content of both |
| Both sides changed that order differently | Conflict about the order only; no content is lost either way |
| Identity and time stamp attributes differ (`UUID`, `T`, `S`) | Not a difference |

How it stays safe:

- The result is your file with their changes copied in as bytes from their file. Nothing is re-written or re-formatted, so vendor extensions, comments and layout survive, and the diff stays small.
- Before the result is written it is parsed again and its content is compared with what the merge decided. If the two differ there is no result, and as merge driver it falls back to git's own text merge.
- A conflict that is not decided yet keeps your side in the file. The file is valid XML at every moment.
- Changes that merge cleanly can still contradict each other, for example two blocks that both took the next free identifier. That is why validation runs after the merge, and why the report says so.
- Everything is written down in `.ecuharness/merge/report.md`: each change taken from their side, each conflict, who decided it and when.

It merges any ARXML, not only ECUC values. Inside ECUC values the order of entries carries no meaning; elsewhere it can, so there it is merged like a value.

With the merge driver a conflicted file has no conflict markers, so it looks normal. `ecu_overview` asks git whenever a merge, rebase or cherry-pick is in progress and lists the `.arxml` files that are not settled, so the assistant knows to call `ecu_merge`.

## A new customer file

`ecu_compare` answers "what changed in the new DBC" without anyone reading two files side by side:

```
/BodyStatus               MESSAGE  changed  DLC: 8 -> 6
                          in project: IPdu_BodyStatus [ComIPdu], Pdu_BodyStatus [Pdu]
/BodyStatus/DoorState     SIGNAL   changed  LENGTH: 4 -> 3
                          in project: Sig_DoorState [ComSignal]
/BodyStatus/WindowState   SIGNAL   added    START-BIT=8, LENGTH=4, ...
```

It compares DBC files and any ARXML file (system description, ECU extract, software components, configuration). Without `old` it compares with the last committed version of the same path, which is the usual case when a new delivery overwrote the old file. "In project" is a name match to start from, not a derivation.

In a DaVinci Configurator project the configuration is derived from the input files by the vendor tool. The harness does not imitate that. The project owner can approve the tool's own update as a task:

```json
"policy": {
  "scriptTasks": {
    "update-inputs": {"description": "Update the project from its input files",
                      "command": ["project", "update"]}
  }
}
```

The words in `command` are passed to the vendor command line, followed by the project and BSW package options. The assistant can then run exactly this task, with a goal. The run happens inside a change, so what the tool rewrote can be reviewed (`ecu_change_diff`), validated and undone. Which commands your installation offers is shown by its own help; `ecuh probe` records it.

## Approval, audit and undo

```json
"policy": {"approvalPaths": ["/ActiveEcuC/Os/*", "/ActiveEcuC/Dcm/*"]}
```

A change that touches these paths can be edited and validated as usual, but it cannot be finished until a person has looked at it and typed `ecuh change approve` in a terminal. The approval covers exactly the file contents at that moment; a later edit needs a new one. The assistant has no tool for approving and is told never to run the command. This is a deliberate step in the workflow, not a security boundary: someone who controls your terminal can type what you can type.

Every finished change leaves three files in `.ecuharness/changes/<id>/`:

- `report.md` for the reviewer: goal, what changed, validation before and after, checks, files;
- `audit.json` for other tools: the same facts, with file hashes before and after, findings, git branch and commit, user, approval;
- `journal.jsonl`: every operation in order. The files as they were are in `snapshot/`.

The result of `ecu_change_finish` carries one sentence that the assistant passes on:

```
pass: structure, types and ranges, vendor validation (dvcfg6). not run: xsd schema (no schema
validator in the harness), code generation, build and tests (outside the harness).
```

`ecuh change undo <id>` restores the files of a finished change, as long as nothing touched them since.

## Cost and speed (measured)

Context cost on the demo project:

| | Calls | Size |
|---|---|---|
| Tool definitions, sent with each request | | 6,593 characters, about 1,650 tokens. In a git repository `ecu_merge` adds 574, with a vendor tool `ecu_generate` adds 169 |
| "Add a signal that does not overlap", done directly | 3 | 1,536 characters of results, about 385 tokens |
| The same kind of task with a model that stumbles: forgets the goal, misspells a parameter, causes two findings, tries to finish too early | 6 | 2,903 characters, about 725 tokens |
| Merge two branches: one real conflict, one contradiction that only validation finds | 4 | 2,039 characters, about 510 tokens |
| Follow a new customer DBC: compare, three edits, finish | 3 | 2,350 characters, about 590 tokens |

Why it is short:

- One `ecu_find` call can answer a question: `show` adds parameter values, `targets_of` and `users_of` follow references across modules, `count_by` counts.
- One `ecu_edit` call carries the goal and up to 50 operations, opens the change, and returns the findings the change introduced. No separate begin or validate call is needed.
- `ecu_change_finish` returns what changed and what was checked, so no extra call is needed to report.
- A merge returns only the conflicts a person must decide, never the XML.
- Results are compact, lists are capped with a count of what was left out, and each result ends with one line on what to do next.

`ecuh tools` prints the current definition size; a test fails if it grows past its budget.

Speed on one laptop (Windows, Python 3.10), one configuration file of the given size, including the checks that run inside each step:

| File | Containers | Load | Find / get | One verified edit | Finish | Three-way merge |
|---|---|---|---|---|---|---|
| 8 MB | 5,000 | 0.4 s | under 0.1 s | 0.3 s | 0.5 s | 1.4 s, 65 MB of memory |
| 31 MB | 20,000 | 1.3 s | 0.1 s | 1.4 s | 1.9 s | 7 s, 200 MB |
| 115 MB | 75,000 | 4.5 s | under 0.5 s | 5.3 s | 6.8 s | 29 s, 700 MB |

The server starts loading in the background as soon as the host starts it, and reports progress during long calls to hosts that ask for it. A vendor tool driver adds the tool's own run time when a change is opened, validated and finished; it is not run inside each edit unless you set `driver.validateOnEdit`.

## Works with weak and strong models

- Tool schemas use only the JSON Schema subset every provider accepts: no unions, no free-form objects, typed array items. A test enforces it.
- Typical slips are repaired instead of rejected: numbers sent as text, one operation sent instead of a list, a list sent as a JSON string or as plain text, a list of paths where text was expected, wrong letter case in names and enum values, a stray change id.
- Every error says what was wrong and what to do: the valid names, the allowed range, the free bit ranges, valid reference targets.
- The harness, not the model, keeps the state: the open change, its snapshots, its journal and the open merge conflicts are on disk, so nothing depends on the model's memory or on the connection.
- Procedures come three ways: as an Agent Skill (`.github/skills/ecu-config`), as MCP prompts (slash commands), and through `ecu_recipe`. Put your team's own procedures as Markdown files into an `ecu-recipes` folder in the project.

To see how a given model does, use the evaluation tasks. They grade the final state of the files, not the model's words:

```
python ecuh.py eval list
python ecuh.py eval setup add-signal C:\tmp\task1     # makes a workspace and prints the prompt
# open that folder in VS Code, pick the model, paste the prompt
python ecuh.py eval grade add-signal C:\tmp\task1
```

Eleven tasks cover setting values, adding containers with dependent settings, fixing seeded errors, renaming, removing something still in use, respecting a read-only policy, ignoring instructions hidden in the data, following a new customer DBC, merging two branches, and stopping for a person's approval. `ecuh eval run` runs reference solutions to prove the tasks and graders themselves.

## Safety

- **Edits only inside a change.** A file is copied before its first modification; discarding the change restores the exact bytes.
- **All or nothing.** If any operation of a call is invalid, nothing is written.
- **Checked before writing.** Types, ranges, enum values, multiplicities, short names and reference targets are checked against the module definitions. Renaming updates every reference; removing is refused while something still refers to the container.
- **Proven after writing.** The new file content is parsed again and compared with the intended result before it replaces the file. If they differ, nothing is written. This holds for edits and for merges.
- **Untouched bytes stay untouched.** An edit changes only the lines it concerns. Line endings, indentation, comments, annotations and UUID style are kept, so the diff in your version control stays small.
- **A gate, not an opinion.** A change can only be finished when it introduced no new errors compared with the state when it began, and the tool driver actually ran. Findings that existed before do not block. A vendor tool that crashes or is missing is never read as "valid".
- **Said plainly.** Each finished change lists what was checked and what was not. Nothing is called valid that was not validated.
- **Outside edits are caught.** If an ARXML file changes without going through the harness while a change is open, the harness stops and says so. Only a person can accept that (`ecuh change accept-external`). Files the vendor tool rewrites during its own run are recorded and stay undoable.
- **Project policy** in `ecuharness.json`: read-only modules, protected paths, paths that need a person's approval, a limit on removals per change, masked parameters whose values are never shown to the AI, and a list of approved vendor tool tasks.
- **Traceable.** Every change has a goal, an optional ticket id, a journal of all operations, a report and an audit record. Every tool call is written to `.ecuharness/log/`.
- **Nothing leaves the PC through the harness.** No network code, no telemetry, no shell commands, no third-party packages; a test enforces this. It starts two programs, both without a shell and with fixed arguments: the vendor command line you configured, and `git`, which it uses to read (conflicted files, the last commit, branch and commit) and, only when you ask for the merge driver, to set that in your clone. What does leave the PC is what your AI host sends to its model: tool inputs and outputs, meaning names and values of the elements the assistant looked at. Mask sensitive parameters with `policy.maskedParams`.

The harness is a development aid, not a qualified tool. Its structural checks do not replace the vendor tool's validation, and a person reviews every change.

## When MCP servers are not allowed

Some company PCs do not allow MCP servers. The same tools then run in the terminal, and the generated skill tells the assistant how:

```
.ecuharness\ecuh.cmd call ecu_find kind=ComSignal show=ComBitPosition,ComBitSize
.ecuharness\ecuh.cmd call ecu_edit @edit.json
```

Arguments without spaces are written as `name=value`; everything else goes into a JSON file passed as `@file`, so no quoting is needed in any shell. `init` creates the command in the project (`.ecuharness\ecuh.cmd` for PowerShell and cmd, `.ecuharness/ecuh` for bash), and `doctor` tests it.

## Limits

- The vendor tool drivers are unconfirmed (see Status), and so is how a vendor tool reads a file the harness merged. The work-PC check covers both.
- Editing covers ECUC values. Instance references can be read, not written.
- There is no validation against the AUTOSAR XSD. The harness checks structure, references and the module definitions; the schema is checked by the vendor tool when it loads the project.
- Deriving configuration from DBC or system description files is not done here. The harness compares the input and runs the vendor tool's update when you approved it as a task.
- A merge works on one file at a time, as git does. It cannot see that a change in one file contradicts a change in another; validation after the merge does. The order of entries inside ECUC values is treated as meaningless, which is how the standard defines them.
- Variant projects: a parameter that carries several values for different variants is refused by `set`, to avoid damaging variants. The merge keeps the values of different variants apart by their variation point.
- Task timing, stack use and CPU load are not in the configuration. The harness gives the configured facts; it does not estimate measurements.
- Whether a vendor tool keeps a value the harness changed on a parameter it derives from input files is not known yet; the work-PC check covers it. Values marked as calculated by the tool (IS-AUTO-VALUE) are changed with a warning.
- A container that is split over several files is not joined when the project is loaded; the second copy is reported and ignored.
- Only UTF-8 files are edited. Files in UTF-16 are not merged or compared.
- Renaming a container that already had a validation error makes that error count as new, because its path changed.

## Commands

```
ecuh init | doctor | demo <dir>            set up, check, create the demo project
ecuh tools                                 list tools and their context size
ecuh call <tool> name=value ...            call a tool from the shell
ecuh merge [--theirs ids] [--ours ids]     merge conflicted *.arxml by element; decide conflicts
ecuh merge --install-driver                let git merge *.arxml this way in this clone
ecuh change list                           the changes of this project
ecuh change approve | undo <id> | accept-external     decisions of a person
ecuh probe [--validate]                    record how the vendor command line behaves
ecuh eval list | run | setup | grade       evaluation tasks
ecuh mcp                                   the server; started by the AI host
```

`ecuh` stands for `python <path>/ecuh.py`, `python <path>/ecuh.pyz`, or the project's own `.ecuharness/ecuh`.

## Development

```
python -m unittest discover -s tests       # 137 tests, about forty seconds; some need git
python tools/build_pyz.py                  # dist/ecuh.pyz
```

Design notes are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), changes per version in [CHANGELOG.md](CHANGELOG.md). License: MIT.

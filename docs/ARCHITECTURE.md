# Architecture

## The idea

A language model is good at understanding a request and bad at being a reliable XML editor, a type checker, a merge tool or a record keeper. So the harness keeps every job that must be exact in ordinary code, and leaves the model the job of deciding what to do.

| Job | Who does it |
|---|---|
| Understand the request, choose the steps | the model |
| Know what a parameter allows | the definitions, served by `ecu_describe` |
| Change the file correctly | the edit engine |
| Merge two branches | the merge engine; a person decides the real conflicts |
| Say what changed in an input file | the compare engine |
| Decide whether the result is valid | structural checks and the vendor tool |
| Remember what was done | the change journal and the audit record on disk |
| Decide whether it is acceptable | the engineer, with the report; for listed paths by explicit approval |

Two consequences drive the tool design. A weak model must not be able to do damage, so everything it sends is checked and repaired or refused with a concrete fix. A strong model must not waste tokens, so the common task is three calls and no call returns more than it must.

A long instruction text cannot do this. A rule such as "never claim success unless the checks passed" is a hope when it stands in a prompt and a fact when the tool refuses to finish and writes down which checks ran.

## Layers

```
AI host (VS Code, Copilot app, ...)          terminal (no MCP allowed)        git (merge driver)
      |  MCP over stdio                            |  ecuh call <tool> name=value     |  ecuh merge-file
mcp.py          protocol only: both eras     cli.py ---------------------------------+
      \____________________  ______________________/
tools.py        the tools: argument repair, compact results, hints
changes.py      change sessions: snapshot, baseline, gate, approval, journal, report, audit, undo
edit.py         operations -> draft -> byte-exact text edits -> proof
gitmerge.py     conflicted files in a repository: merge, open conflicts, decisions, report
merge.py        three-way merge by element -> byte splices -> proof
compare.py      two versions of an input file (ARXML, DBC) -> rows; names found in the project
check.py        structural rules from the definitions
drivers.py      who else judges: none | sim | dvcfg6 | dvadaptive
project.py      configuration, loaded files, merged model
arxml.py        span-preserving reader for ECUC values and definitions
xtree.py        plain element tree with identity keys, for merging and comparing
gitio.py        the few read commands the harness asks of git
```

## Reader (`arxml.py`)

One pass with the XML parser of the standard library, which reports the byte offset of every tag. Module configurations are read by a streaming state machine that builds containers and values directly and takes element text from the bytes; this is what makes a 100 MB file load in a few seconds. A second, simpler reader (capture a subtree, then convert it) handles module definitions and serves as an independent implementation: a test requires both to agree on every name, value and offset for a file full of awkward XML.

Each container knows the byte spans an edit can need: itself, its name, its value wrappers, each value element. Everything the reader does not interpret (administrative data, annotations, variation points) is skipped, and therefore left alone by edits.

Definition files are loaded lazily: a file is parsed only if it defines a module the project configures.

## Edit engine (`edit.py`)

1. Operations are applied to a *draft* of the touched containers, in order, so later operations see the effect of earlier ones. All checks happen here: definitions, types, ranges, multiplicities, names, reference targets.
2. References follow renames; removals are refused while references remain.
3. From the difference between draft and original, text edits are derived: replace one value element, delete one entry with its line, insert one block with the indentation and line endings of the file.
4. The new bytes are parsed. Every touched container must be found at its expected path with exactly the expected name, definition, values and children. Only then are the files written, each through an atomic replace, after a copy of the original was stored.

If step 4 fails, the caller gets an internal error and no file has changed.

## Element tree (`xtree.py`)

Merging and comparing must work on any ARXML, so they use a reader that interprets nothing. It keeps elements with their byte spans and gives every child an identity among its siblings:

| Child | Identified by |
|---|---|
| has a SHORT-NAME | tag and name |
| ECUC value (DEFINITION-REF, no name) | tag and definition; plus the value when the parameter holds several; plus the variant when it carries a variation point |
| has a GID attribute | tag and GID |
| its tag occurs once among the siblings | the tag |
| anything else | its content (a list item) |

Every element gets a content hash while the file is read, one that ignores the order of children and one that includes it. `UUID`, `T` and `S` attributes are left out of the hash: they are identity and bookkeeping, not content.

Memory: the children of an element shorter than 4 kB are dropped again as soon as its hash is known. The element stays, with hash and span. Versions of a file differ in few places, and only there are the children needed; they are then read a second time from the span, and the hash of the second reading must equal the first. A 115 MB file is merged with about 700 MB of memory, most of it the three versions themselves.

## Merge engine (`merge.py`)

Input: the common ancestor, ours, theirs. The three trees are walked together, matched by key.

- Their side did not change an element (same hash as the ancestor): keep ours, do not look inside.
- Both sides have the same content: keep ours.
- A leaf that only they changed: take theirs. That both changed differently: conflict.
- Children: only they added one: insert it. Only they removed one that we did not touch: remove it. Removed on one side and changed on the other: conflict. Added on both sides under the same name with different content: conflict.
- Inside ECUC values the order of entries carries no meaning and is ignored. Elsewhere the order of the entries both sides kept is merged like a value: the side that changed it wins; both changed it differently: conflict. The content of both sides is kept either way, so deciding an order conflict never drops a change.
- Without any common ancestor (both sides added the file) the two versions are joined, and only differing values are conflicts.

The result is produced as edits on *our* bytes: replace this span with those bytes of their file, insert their element after that sibling, delete this element with its line. Nothing is generated. A new child goes after the sibling that precedes it on their side, and after children only we have whose tag must come first; that order is known for the ECUC wrappers and learned for other tags from the order the three files themselves use. When their order is taken, the element's content is put together from its merged children in their order, each child with the bytes that stood before it in its own file (line break, indentation, comments).

Proof: while it decides, the merge computes the content hash the merged file must have. The spliced bytes are parsed again; if their hash differs, there is no result. A second safety net are the laws a test checks on random edits of both sides: if we changed nothing the result is theirs; if they changed nothing not one byte of ours moves; deciding every conflict for one side gives the same content whichever file is called "ours"; merging a second time changes nothing.

A conflict is a small record: where (AUTOSAR path), which parameter or field, base, ours, theirs. A conflict without a decision keeps our side, so the file is valid at every moment. Decisions are applied by merging again from the three versions with the decisions as input; there is no bookkeeping of positions.

## Merging in a repository (`gitmerge.py`, `gitio.py`)

When git stops on a conflict it keeps the three versions of the file in its index. `ecu_merge` and `ecuh merge` read them (with the line endings git would check out), merge, and replace the working file, which at that point holds conflict markers. A copy of what was replaced goes into the git folder. A working file that has neither markers nor the harness's own result was edited by a person and is left alone until `--redo`.

Open conflicts and decisions live in `ecuh-merge.json` inside the git folder, so they belong to the clone and survive restarts. Their side can only be written into a file that is still exactly as the merge left it; otherwise the tool says to set the value with `ecu_edit` and mark the conflict as decided.

As merge driver (`ecuh merge-file %O %A %B`, set up by `ecuh merge --install-driver`) the same merge runs inside `git merge`, `git rebase` and `git cherry-pick`. Exit code 0 means clean; 1 means conflicts are left, and git marks the file as conflicted while its content stays valid XML with our side for the open points. If the merge by element cannot be done or proven, the driver runs git's own text merge, so it is never worse than no driver.

The harness asks git only to read, with two exceptions that a person triggers: setting the driver in the clone's config, and the text-merge fallback on git's temporary files. It never stages and never commits.

## Compare engine (`compare.py`)

Two versions of a file are walked with the same keys. The differences are grouped into one row per named element: added (with its first values), removed, renamed (a removal and an addition with identical content under the same parent), or changed (which field, old and new value).

A DBC file is first rewritten as a small XML document of messages, signals and their properties, so the same comparison serves it. That includes the attributes of messages, signals, nodes and the bus itself (cycle times, the baud rate), value tables and float signal types. Numbers are normalised (`8.0` is `8`), receivers and value tables are sorted, so a re-export alone is not a change.

For an input file the tool adds, per row, the configuration containers whose short name contains the element's name. This is a text match over an index of all container names. It tells where to look; it does not derive configuration.

## Change sessions (`changes.py`)

State is on disk under `.ecuharness/changes/<id>/`: `change.json`, `baseline.json`, `journal.jsonl`, `snapshot/`, `report.md`, `audit.json`. One change is open at a time, so no tool needs a change id.

- **Baseline**: all findings when the change began. Validation compares against it, so only findings the change introduced count. A vendor tool can need minutes, so the baseline takes over the tool's last result when the ARXML files and the driver settings are still exactly as they were then, for at most two hours; finishing and `ecu_validate` always run the tool. Files the harness does not read (the BSW package, input files the tool reads by itself) are not part of that comparison: after changing those, run `ecu_validate` once before the next change.
- **Gate**: no new errors (or no new findings at all with `policy.gate: "no-new-findings"`), and the tool driver completed.
- **Approval**: if a changed path matches `policy.approvalPaths`, finishing is refused until a person ran `ecuh change approve`. The approval stores the content hashes of the files; any later modification voids it. The command asks for the change id on the keyboard and refuses when there is no keyboard.
- **Hashes**: the expected content hash of every editable file. A mismatch means someone edited outside the harness; everything stops until a person decides.
- **Tool runs**: a vendor tool may rewrite configuration files while it validates, generates or runs a task. The harness compares the files before and after each run, keeps the previous bytes as snapshot, and records what the tool changed.
- **Checks ledger**: at finish, every check is listed as pass, fail or not run: structure, types and ranges, XSD schema (never run: there is no validator), vendor validation, code generation (pass only if it ran after the last edit), build and tests (outside the harness).
- **Report and audit record**: goal, the agent's summary, the diff computed from snapshot and current files, validation before and after, checks, file hashes, git branch and commit, approval.
- **Undo**: `ecuh change undo <id>` restores the snapshot of a finished change if every file still has the hash the change left.
- **Lock**: an operating-system file lock around every writing call. The system releases it if the process dies.

## Server (`mcp.py`)

Hand-written JSON-RPC over stdio, about 200 lines, no dependency. It answers the stateless protocol of 2026-07-28 (`server/discover`, version in every request) and the older `initialize` handshake. It holds no conversation state, as the newer protocol requires. Parsing starts in a background thread when the server starts.

## Tool design (`tools.py`)

- Few tools with one job each; read tools are marked read-only so hosts need not ask for confirmation.
- Flat arguments, all values as text; the harness converts and checks them.
- The write path is short: `ecu_edit` opens the change when given a goal, and returns the findings the edit introduced. Structural checks always run there; the vendor tool runs there only for fast drivers or with `driver.validateOnEdit`, otherwise at `ecu_validate` and `ecu_change_finish`.
- Results: compact JSON, lists capped with a count of what was left out, a `next` hint.
- Errors are results the model can read (`error`, `hint`, sometimes the findings), not protocol failures.
- A merge returns the conflicts, never XML. A comparison returns rows, never files.

## Settings (`ecuharness.json`)

```json
{
  "name": "MyEcu",
  "model": {"config": [], "definitions": [], "readonly": []},
  "driver": {"type": "none", "exe": null, "bswPackage": null, "project": null,
             "timeoutSeconds": 1800, "reportGlobs": ["**/*.sarif"], "validateOnEdit": false},
  "policy": {
    "gate": "no-new-errors",
    "maxOpsPerEdit": 50,
    "maxRemovalsPerChange": 25,
    "readOnlyModules": [],
    "protectedPaths": [],
    "approvalPaths": [],
    "allowGenerate": true,
    "scriptTasks": {},
    "maskedParams": ["*Password*", "*Passphrase*", "*Secret*", "*KeyElementInitValue*"]
  },
  "checks": {"foreignRefs": false},
  "tools": {"off": []},
  "recipes": ["ecu-recipes"]
}
```

- `protectedPaths`, `approvalPaths` and `maskedParams` take `*` patterns. A protected path cannot be edited at all; a path under approval can, but a person must approve before the change is finished.
- `scriptTasks` maps a task name to what the vendor command line shall run. Either `{"description": "...", "location": "<folder of the script>"}` for an automation script task, or `{"description": "...", "command": ["project", "update"]}` for words of the command line itself. The project and BSW package options are appended; write `{project}` and `{bsw}` inside the words to place them yourself. Only listed tasks can be run, and command tasks take no arguments from the model.
- `tools.off` lists tools this project does not need. They are then not offered to the model at all.
- `driver.project` is the project file when the tool wants it instead of the folder (`.dvjson`, `.adpa`).
- Policy does not apply to a merge: a merge brings in work a colleague already committed.

## Adding a driver

A driver is a class with `info`, `validate`, `generate` and `run_script`, each returning a `DriverResult` (status `ok`, `failed` or `unavailable`, findings, detail, exit code). Rules that keep it honest:

- Run the tool without a shell and with fixed arguments.
- Never turn a failure into success: if the tool did not run, or returned an error that nothing machine-readable explains, the status is `failed` and the gate stays closed.
- Do not guess silently. Where the documentation leaves a choice open, try the documented form first, fall back on a documented signal (for `dvcfg-b`: exit code 2, usage error), and say in `detail` what worked.

## Extending coverage

- **Configurator Classic 5**: a driver around its command line; the file layer, merge and compare already apply.
- **Editing through the tool's own automation interface** instead of files: expose approved tasks (`policy.scriptTasks`) today; a generic bridge is future work and needs the tool at hand.
- **Editing other ARXML** (software components, system, Adaptive manifests): they can be searched, read, compared and merged now. Editing needs the AUTOSAR schema as the source of truth, the way the module definitions are for ECUC.
- **Schema validation**: an XSD validator cannot be written from the standard library alone. A driver around an external validator would fit the existing driver interface.

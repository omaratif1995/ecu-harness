# Architecture

## The idea

A language model is good at understanding a request and bad at being a reliable XML editor, a type checker or a record keeper. So the harness keeps every job that must be exact in ordinary code, and leaves the model the job of deciding what to do.

| Job | Who does it |
|---|---|
| Understand the request, choose the steps | the model |
| Know what a parameter allows | the definitions, served by `ecu_describe` |
| Change the file correctly | the edit engine |
| Decide whether the result is valid | structural checks and the vendor tool |
| Remember what was done | the change journal on disk |
| Decide whether it is acceptable | the engineer, with the report |

Two consequences drive the tool design. A weak model must not be able to do damage, so everything it sends is checked and repaired or refused with a concrete fix. A strong model must not waste tokens, so the common task is three calls and no call returns more than it must.

## Layers

```
AI host (VS Code, Copilot app, ...)          terminal (no MCP allowed)
      |  MCP over stdio                            |  ecuh call <tool> name=value
mcp.py          protocol only: both eras     cli.py
      \____________________  ______________________/
tools.py        the tools: argument repair, compact results, hints
changes.py      change sessions: snapshot, baseline, gate, journal, report
edit.py         operations -> draft -> byte-exact text edits -> proof
check.py        structural rules from the definitions
drivers.py      who else judges: none | sim | dvcfg6 | dvadaptive
project.py      configuration, loaded files, merged model
arxml.py        span-preserving reader
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

## Change sessions (`changes.py`)

State is on disk under `.ecuharness/changes/<id>/`: `change.json`, `baseline.json`, `journal.jsonl`, `snapshot/`, `report.md`. One change is open at a time, so no tool needs a change id.

- **Baseline**: all findings when the change began. Validation compares against it, so only findings the change introduced count.
- **Gate**: no new errors (or no new findings at all with `policy.gate: "no-new-findings"`), and the tool driver completed.
- **Hashes**: the expected content hash of every editable file. A mismatch means someone edited outside the harness; everything stops until a person decides.
- **Tool runs**: a vendor tool may rewrite configuration files while it validates or generates. The harness compares the files before and after each run, keeps the previous bytes as snapshot, and records what the tool changed.
- **Report**: goal, the agent's summary, the diff computed from snapshot and current files, validation before and after, file hashes.
- **Lock**: an operating-system file lock around every writing call. The system releases it if the process dies.

## Server (`mcp.py`)

Hand-written JSON-RPC over stdio, about 200 lines, no dependency. It answers the stateless protocol of 2026-07-28 (`server/discover`, version in every request) and the older `initialize` handshake. It holds no conversation state, as the newer protocol requires. Parsing starts in a background thread when the server starts.

## Tool design (`tools.py`)

- Few tools with one job each; read tools are marked read-only so hosts need not ask for confirmation.
- Flat arguments, all values as text; the harness converts and checks them.
- The write path is short: `ecu_edit` opens the change when given a goal, and returns the findings the edit introduced. Structural checks always run there; the vendor tool runs there only for fast drivers or with `driver.validateOnEdit`, otherwise at `ecu_validate` and `ecu_change_finish`.
- Results: compact JSON, lists capped with a count of what was left out, a `next` hint.
- Errors are results the model can read (`error`, `hint`, sometimes the findings), not protocol failures.

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
    "allowGenerate": true,
    "scriptTasks": {},
    "maskedParams": ["*Password*", "*Passphrase*", "*Secret*", "*KeyElementInitValue*"]
  },
  "checks": {"foreignRefs": false},
  "recipes": ["ecu-recipes"]
}
```

`protectedPaths` and `maskedParams` take `*` patterns. `scriptTasks` maps a task name to `{"description": "...", "location": "<folder of the script>"}`; only listed tasks can be run. `driver.project` is the project file when the tool wants it instead of the folder (`.dvjson`, `.adpa`).

## Adding a driver

A driver is a class with `info`, `validate`, `generate` and `run_script`, each returning a `DriverResult` (status `ok`, `failed` or `unavailable`, findings, detail, exit code). Rules that keep it honest:

- Run the tool without a shell and with fixed arguments.
- Never turn a failure into success: if the tool did not run, or returned an error that nothing machine-readable explains, the status is `failed` and the gate stays closed.
- Do not guess silently. Where the documentation leaves a choice open, try the documented form first, fall back on a documented signal (for `dvcfg-b`: exit code 2, usage error), and say in `detail` what worked.

## Extending coverage

- **Configurator Classic 5**: a driver around its command line; the file layer already applies.
- **Editing through the tool's own automation interface** instead of files: expose approved script tasks (`policy.scriptTasks`) today; a generic bridge is future work and needs the tool at hand.
- **Other ARXML** (software components, system, Adaptive manifests): searchable and readable now. Editing needs the AUTOSAR schema as the source of truth, the way the module definitions are for ECUC.

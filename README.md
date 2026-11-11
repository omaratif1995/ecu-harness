# ECU Harness

[![tests](https://github.com/omaratif1995/ecu-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/omaratif1995/ecu-harness/actions/workflows/tests.yml)

An AI agent harness for AUTOSAR ECU configuration.

It gives an AI assistant in VS Code or the GitHub Copilot app a small set of tools to read and change ECU configuration (ARXML) safely. Every edit is type-checked, written byte-exactly, proven by re-reading the result, validated against a baseline, and can be undone. The assistant never touches the XML itself.

The point of a harness is that the exact work is done by code, not by the model. A weak model gets reliable because it cannot make the mistakes the tools prevent. A strong model gets cheap because it does not spend tokens finding out how the task is done.

It works on Vector DaVinci Configurator Classic projects and on any other project that stores its ECU configuration as AUTOSAR ECUC ARXML.

> Independent open-source project. Not affiliated with or endorsed by Vector Informatik GmbH or the AUTOSAR partnership. DaVinci and MICROSAR are trademarks of Vector Informatik GmbH; AUTOSAR is a trademark of the AUTOSAR partnership. The repository contains no vendor files, definitions or documentation.

## Status: what is proven and what is not

| Part | Status |
|---|---|
| Read, search and edit ECUC values (containers, parameters, references) | Tested: 73 automated tests, byte-exact edits, two independent readers cross-checked |
| Change sessions: snapshot, baseline, gate, review report, undo | Tested |
| MCP server (protocol 2026-07-28, and 2025-11-25 and older) | Tested with a protocol-level client. Not yet confirmed by a person inside VS Code or the Copilot app |
| Driver for DaVinci Configurator Classic 6 (`dvcfg-b`) | Experimental. Written from Vector's public documentation and tested against a stand-in executable only. Confirm it with [docs/WORK-PC-CHECK.md](docs/WORK-PC-CHECK.md) |
| Driver for DaVinci Developer Adaptive (`DvCliAdaptive`) | Experimental, same caveat |
| Other ARXML: software components, system descriptions, Adaptive manifests | Can be searched and read. Cannot be edited |
| DaVinci Configurator Classic 5, DaVinci Developer Classic | Their ECUC files can be read and edited at file level. No tool driver yet |
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
   - "Rename PDU Pdu_EngineData to Pdu_Engine."

For a PC where only single files may be copied: `python tools/build_pyz.py` makes `dist/ecuh.pyz`, the whole harness in one file of about 85 kB that is used the same way (`python ecuh.pyz init`). A `pyproject.toml` is included for `pip install .`, which is not tested yet.

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

Ten tools, plus two that appear only when they are usable.

| Tool | Job |
|---|---|
| `ecu_overview` | Modules, tool driver, open change |
| `ecu_find` | Search by name, kind, parameter value or reference relation; `show` returns parameter values with the matches |
| `ecu_get` | Read elements: values, references, children, who refers to them. Several paths in one call |
| `ecu_describe` | What a kind or parameter allows: type, range, allowed values, required |
| `ecu_edit` | Operations: set, append, unset, add, clone, rename, remove. Opens the change, checks every op, reports new findings |
| `ecu_validate` | Full validation; findings this change introduced; whether it may be finished |
| `ecu_change_diff` | What the change modified, computed from the files |
| `ecu_change_finish` | Validate, close the change, write the review report |
| `ecu_change_discard` | Undo the change |
| `ecu_recipe` | Step-by-step procedures for common tasks |
| `ecu_generate` | Run the vendor code generator (only with a driver) |
| `ecu_script_run` | Run a script task the project owner approved (only if any is approved) |

The shortest path is three calls: look (`ecu_find`), change (`ecu_edit` with a goal), close (`ecu_change_finish`). You then read `.ecuharness/changes/<id>/report.md`, look at the diff in your version control, and commit yourself. The harness never commits.

## Cost and speed (measured)

Context cost on the demo project:

| | Calls | Size |
|---|---|---|
| All tool definitions, sent with each request | | 5,839 characters, about 1,460 tokens |
| "Add a signal that does not overlap", done directly | 3 | 1,272 characters of results, about 320 tokens |
| The same kind of task with a model that stumbles: forgets the goal, misspells a parameter, causes two findings, tries to finish too early | 6 | 2,639 characters, about 660 tokens |

Why it is short:

- One `ecu_find` call can answer a question: `show` adds parameter values, `targets_of` and `users_of` follow references across modules.
- One `ecu_edit` call carries the goal and up to 50 operations, opens the change, and returns the findings the change introduced. No separate begin or validate call is needed.
- `ecu_change_finish` returns what changed, so no extra call is needed to report.
- Results are compact, lists are capped with a count of what was left out, and each result ends with one line on what to do next.

`ecuh tools` prints the current definition size; a test fails if it grows past its budget.

Speed on one laptop (Windows, Python 3.10), one configuration file of the given size, including the checks that run inside each edit:

| File | Containers | Load | Find / get | One verified edit | Finish |
|---|---|---|---|---|---|
| 8 MB | 5,000 | 0.4 s | under 0.1 s | 0.3 s | 0.5 s |
| 31 MB | 20,000 | 1.3 s | 0.1 s | 1.4 s | 1.9 s |
| 115 MB | 75,000 | 4.5 s | under 0.5 s | 5.3 s | 6.8 s |

The server starts loading in the background as soon as the host starts it, and reports progress during long calls to hosts that ask for it. A vendor tool driver adds the tool's own run time when a change is opened, validated and finished; it is not run inside each edit unless you set `driver.validateOnEdit`.

## Works with weak and strong models

- Tool schemas use only the JSON Schema subset every provider accepts: no unions, no free-form objects, typed array items. A test enforces it.
- Typical slips are repaired instead of rejected: numbers sent as text, one operation sent instead of a list, a list sent as a JSON string, a list of paths where text was expected, wrong letter case in names and enum values, a stray change id.
- Every error says what was wrong and what to do: the valid names, the allowed range, the free bit ranges, valid reference targets.
- The harness, not the model, keeps the state: the open change, its snapshots and its journal are on disk, so nothing depends on the model's memory or on the connection.
- Procedures come three ways: as an Agent Skill (`.github/skills/ecu-config`), as MCP prompts (slash commands), and through `ecu_recipe`. Put your team's own procedures as Markdown files into an `ecu-recipes` folder in the project.

To see how a given model does, use the evaluation tasks. They grade the final state of the files, not the model's words:

```
python ecuh.py eval list
python ecuh.py eval setup add-signal C:\tmp\task1     # makes a workspace and prints the prompt
# open that folder in VS Code, pick the model, paste the prompt
python ecuh.py eval grade add-signal C:\tmp\task1
```

Eight tasks cover setting values, adding containers with dependent settings, fixing seeded errors, renaming, removing something still in use, respecting a read-only policy, and ignoring instructions hidden in the data. `ecuh eval run` runs reference solutions to prove the tasks and graders themselves.

## Safety

- **Edits only inside a change.** A file is copied before its first modification; discarding the change restores the exact bytes.
- **All or nothing.** If any operation of a call is invalid, nothing is written.
- **Checked before writing.** Types, ranges, enum values, multiplicities, short names and reference targets are checked against the module definitions. Renaming updates every reference; removing is refused while something still refers to the container.
- **Proven after writing.** The new file content is parsed again and compared with the intended result before it replaces the file. If they differ, nothing is written.
- **Untouched bytes stay untouched.** An edit changes only the lines it concerns. Line endings, indentation, comments, annotations and UUID style are kept, so the diff in your version control stays small.
- **A gate, not an opinion.** A change can only be finished when it introduced no new errors compared with the state when it began, and the tool driver actually ran. Findings that existed before do not block. A vendor tool that crashes or is missing is never read as "valid".
- **Outside edits are caught.** If an ARXML file changes without going through the harness while a change is open, the harness stops and says so. Only a person can accept that (`ecuh change accept-external`). Files the vendor tool rewrites during its own run are recorded and stay undoable.
- **Project policy** in `ecuharness.json`: read-only modules, protected paths, a limit on removals per change, masked parameters whose values are never shown to the AI, and a list of approved script tasks.
- **Traceable.** Every change has a goal, an optional ticket id, a journal of all operations and a report. Every tool call is written to `.ecuharness/log/`.
- **Nothing leaves the PC through the harness.** No network code, no telemetry, no shell commands, no third-party packages; a test enforces this. What does leave is what your AI host sends to its model: tool inputs and outputs, meaning names and values of the elements the assistant looked at. Mask sensitive parameters with `policy.maskedParams`.

The harness is a development aid, not a qualified tool. Its structural checks do not replace the vendor tool's validation, and a person reviews every change.

## When MCP servers are not allowed

Some company PCs do not allow MCP servers. The same tools then run in the terminal, and the generated skill tells the assistant how:

```
.ecuharness\ecuh.cmd call ecu_find kind=ComSignal show=ComBitPosition,ComBitSize
.ecuharness\ecuh.cmd call ecu_edit @edit.json
```

Arguments without spaces are written as `name=value`; everything else goes into a JSON file passed as `@file`, so no quoting is needed in any shell. `init` creates the command in the project (`.ecuharness\ecuh.cmd` for PowerShell and cmd, `.ecuharness/ecuh` for bash), and `doctor` tests it.

## Limits

- The vendor tool drivers are unconfirmed (see Status).
- Editing covers ECUC values. Instance references can be read, not written.
- Variant projects: a parameter that carries several values for different variants is refused by `set`, to avoid damaging variants.
- Whether a vendor tool keeps a value the harness changed on a parameter it derives from input files is not known yet; the work-PC check covers it. Values marked as calculated by the tool (IS-AUTO-VALUE) are changed with a warning.
- A container that is split over several files is not merged; the second copy is reported and ignored.
- Only UTF-8 files are edited.
- Renaming a container that already had a validation error makes that error count as new, because its path changed.

## Commands

```
ecuh init | doctor | demo <dir>            set up, check, create the demo project
ecuh tools                                 list tools and their context size
ecuh call <tool> name=value ...            call a tool from the shell
ecuh change list | accept-external         changes; accept an outside edit as a person
ecuh probe [--validate]                    record how the vendor command line behaves
ecuh eval list | run | setup | grade       evaluation tasks
ecuh mcp                                   the server; started by the AI host
```

`ecuh` stands for `python <path>/ecuh.py`, `python <path>/ecuh.pyz`, or the project's own `.ecuharness/ecuh`.

## Development

```
python -m unittest discover -s tests       # 73 tests, about ten seconds
python tools/build_pyz.py                  # dist/ecuh.pyz
```

Design notes are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). License: MIT.

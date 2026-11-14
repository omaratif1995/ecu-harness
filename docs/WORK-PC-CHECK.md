# Checking the harness against the real tool

The drivers for DaVinci Configurator Classic 6 and DaVinci Developer Adaptive were written from Vector's public documentation, on a PC without the tools. This page is the check to run once on a PC that has them. It takes about half an hour and never changes your real project.

Follow your company's rules before copying anything to or from a work PC. The harness is plain Python source (or the single file `ecuh.pyz`), needs only Python 3.8+, and has no network code.

In the commands below, `ecuh` stands for `python <path>\ecuh.py` or `python <path>\ecuh.pyz`.

## 1. Work on a copy

Copy a project folder (or create a scratch project) and do everything below in the copy.

## 2. Set up

```
cd <copy of the project>
ecuh init --bsw-package "<path of the BSW package>"
ecuh doctor
```

`init` prints what it found. Check these lines:

- the number of ECUC value files (they must be your module configuration files);
- the number of module definition files found in the BSW package;
- whether it recognised the DaVinci project and found `dvcfg-b`. If it did not find the executable, run `init` in a fresh copy with `--exe "<full path>\dvcfg-b.exe"`, or put `exe` into `ecuharness.json` yourself.

`doctor`: every line should be `[ok]`. Lines marked `--` are notes, lines marked `!!` are problems and say what to do. The driver line must say `ready`.

## 3. Record how the tool behaves

```
ecuh probe
ecuh probe --validate
```

The first run stores the version and the help text of every command of `dvcfg-b`. It only asks for help texts and never calls a command that acts. The second also runs one real validation and stores its exit code, its run time, and the *shape* of the report files it wrote (keys, not values). The result is `ecuh-probe.json`. Read it before passing it on.

## 4. Read-only test in VS Code or the Copilot app

Ask the assistant:

- "Give me an overview of the ECU configuration."
- "Find the container named <something you know> and show <two of its parameters>."
- "Explain parameter <a parameter you know> of <its container kind>."

Compare the answers with what DaVinci Configurator shows.

If the `ecu-harness` tools do not appear at all, your PC may not allow MCP servers. Then tell the assistant to use the terminal command described in the skill (`.ecuharness\ecuh.cmd call ...`), or try that command yourself.

## 5. One edit, checked in the vendor tool

Ask for a small, harmless change, for example the value of one integer parameter. Then:

1. Look at `.ecuharness/changes/<id>/report.md`.
2. Look at the file diff in your version control: only the lines of that value should differ.
3. Open the copy of the project in DaVinci Configurator. Does it load without complaint? Does the parameter show the new value? Does validation show the same findings the harness reported?
4. Run an update or generation in the tool and look again: is the value still the one the harness set?
5. Ask the assistant to make and then discard a second change, and confirm the files are back to their previous bytes.

## 6. Merge and compare on real files

These two functions do not need the vendor tool to run, but nobody has yet seen what a vendor tool says about a file they produced.

1. Compare: take two versions of a real input file (a DBC, or the ECU extract) and ask the assistant what changed, or run `ecuh call ecu_compare new=<path> old=<path>`. Check a few rows against what you know changed.
2. Merge, in a scratch clone of the copy:
   - make two branches from one commit, change the same module in DaVinci Configurator on both (for example add one container on each side, and change one parameter to two different values), commit both;
   - merge one into the other. Git stops with a conflict;
   - run `ecuh merge`. It should list only the parameter you changed on both sides. Decide it (`ecuh merge --theirs c1`);
   - open the project in DaVinci Configurator. Does it load? Are both containers there? Does validation agree with `ecuh call ecu_validate`?
3. Optional: `ecuh merge --install-driver`, then repeat the merge. Branches without a real conflict should now merge without stopping.
4. If your installation has a command for updating the project from its input files (`dvcfg-b project --help`; `ecuh-probe.json` lists it), approve it as a task in `ecuharness.json` as the README shows, and let the assistant run it with a goal. `ecu_change_diff` should list what the tool rewrote.

## 7. What to note down

These are the open questions. The answers decide what has to change in `drivers.py` and in the merge.

| Question | Where to look |
|---|---|
| Does `dvcfg-b project validate` return a non-zero exit code when the configuration has validation errors, or only when the tool itself fails? | `ecuh-probe.json`, section `validate` |
| Does `-p` take the project folder or the `.dvjson` file? The driver tries the folder first and the file if the tool answers with a usage error; it then says so | `ecu_validate` output, field `driver.detail` |
| Where are the SARIF report files written, and what are they called? The driver looks for any `*.sarif` in the project written during the run; `driver.reportGlobs` can narrow it | `report_files` in the probe file |
| Do the SARIF results carry the AUTOSAR path of the element (logical location) or only file and line? | `shape` of the report file |
| Do solving actions appear in the SARIF file (as `fixes`)? | same |
| Does validation or generation rewrite ECUC files? | the harness reports `config_files_changed_by_tool` |
| Does DaVinci Configurator keep a value the harness changed, also on a parameter it derives from input files, and without rewriting unrelated lines when it saves? | step 5 |
| How long does one validation take with the background process running? | `seconds` in the probe file |
| Does the AI host wait long enough for a slow validation? The first `ecu_edit` of a change and every `ecu_change_finish` run the vendor tool. If the host gives up, the change is still open: ask the assistant to continue | step 5 |
| Does the vendor tool load a file the harness merged, without a model merge error, and does it keep the merged content when it saves? | step 6 |
| Does the tool reorder or re-identify (new UUIDs) containers when it saves? The merge ignores both; a tool that rewrites much more would make every merge large | compare the file before and after a save in your version control |
| Which command updates the project from its input files on your installation, and with which options? | `dvcfg-b project --help`, `ecuh-probe.json` |
| Which files does that update rewrite? | `config_files_changed_by_tool` in the result of `ecu_script_run` |

For DaVinci Developer Adaptive the same procedure applies: `init` recognises the `.adpa` project file and uses `DvCliAdaptive`. Adaptive manifests can be searched and read through the tools; validation and generation go through the driver.

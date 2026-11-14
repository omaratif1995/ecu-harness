# Trying the harness on a real project

The goal of a trial: find out, on your own project, which of your recurring tasks the assistant takes off your hands, and how much time that saves. Plan half a day. Everything here works on a copy of your project; your real project is never touched.

Follow your company's rules for software and data. The harness has no network code and sends nothing anywhere. Your AI host (VS Code with Copilot, the Copilot app) sends what the assistant reads to its model, as it does for any code you open there. Do not post project names, values or files in public places, also not in issues of this repository.

## Before you start (10 minutes)

1. Python 3.8 or newer: `python --version` (or `py --version`). If there is none, ask your IT. Nothing else is installed: the harness uses no other packages.
2. VS Code with GitHub Copilot in agent mode, or the GitHub Copilot app.
3. Get the harness, version 0.3.0 or newer, into a short path:

   ```
   git clone https://github.com/omaratif1995/ecu-harness C:\tools\ecu-harness
   python C:\tools\ecu-harness\ecuh.py --version
   ```

   Without git: download the ZIP from the repository page (Code, Download ZIP) and unpack it to `C:\tools\ecu-harness`.

In the commands below, `ecuh` stands for `python C:\tools\ecu-harness\ecuh.py`.

## 1. Ten minutes with the demo project

```
ecuh demo C:\work\demo-ecu
cd C:\work\demo-ecu
ecuh init
ecuh doctor
```

Open `C:\work\demo-ecu` in VS Code. Open Chat, choose Agent mode and the agent **ECU Engineer**, and trust the `ecu-harness` server when VS Code asks. Then ask:

- "Which signals are in IPdu_EngineData and where do they sit?"
- "Add an 8-bit signal Sig_OilPressure to IPdu_EngineData without overlapping the other signals."
- "The customer sent Input/Vehicle_v2.dbc. What changed compared with Input/Vehicle.dbc, and what does it affect?"

If the assistant shows no `ecu_*` tools, your company may not allow MCP servers. Choose the normal **Agent** instead of ECU Engineer and say: "Use the terminal command `.ecuharness\ecuh.cmd` as the ecu-config skill describes." Everything works that way too, only with more clicks to confirm each command.

## 2. Your project, on a copy (30 minutes)

1. Copy the project folder to a short local path such as `C:\work\trial` (not a network drive), or work on a new git branch of it.
2. Close the project in DaVinci Configurator while the assistant changes files, and open it again afterwards. A tool that still has the project open can save its own state over the changes.
3. Set it up, for now without the vendor tool:

   ```
   cd C:\work\trial
   ecuh init --bsw-package "<path of your BSW package>" --driver none
   ecuh doctor
   ```

   `init` says how many ECUC value files and module definition files it found. The ECUC files must be your module configuration. `doctor` should show `[ok]` on every line except notes (`--`).

   Without the vendor tool, the harness checks structure, references and the module definitions, and you validate in DaVinci Configurator yourself. It is the fastest way to see what the assistant can do. Section 3 adds the vendor tool.
4. Ask a few questions whose answers you know, and compare with DaVinci Configurator:
   - "Give me an overview of the ECU configuration."
   - "Where is <a signal you know> used, in all modules?"
   - "Which tasks exist, with priority and period, and what is mapped to each?"
5. Ask for one small change, for example one parameter value. Then look at `.ecuharness\changes\<id>\report.md` and at the diff in your version control: only those lines may differ. Open the copy in DaVinci Configurator and validate.

## 3. With the vendor tool (optional, 30 minutes)

So that `dvcfg-b` judges every change before it is finished, run sections 2 to 5 of [WORK-PC-CHECK.md](WORK-PC-CHECK.md) in a fresh copy: `init` without `--driver none`, then `ecuh probe` and `ecuh probe --validate`. The probe writes `ecuh-probe.json`, which tells how the tool behaved on your PC.

A validation by the vendor tool takes as long as it takes in DaVinci Configurator. It runs when a change opens and when it is finished. When a change opens right after another one finished, with no ARXML file changed in between, the harness reuses that result instead of running the tool again. After you changed the BSW package or an input file the tool reads, ask for one validation ("validate the project") before the next change.

## 4. Real tasks, timed

Pick the tasks you do most often. Before each one, write down how long it usually takes you. Then let the assistant do it and time it from your request until you have reviewed and validated the result. Count your own reading and checking time in; that is the honest number.

| Kind of task | Example request |
|---|---|
| Find and explain | "Which PDUs carry <signal>, and how is it routed to the bus?" |
| A new element across modules | "Add signal <new> to <PDU>, wired like <existing signal>." |
| Diagnostics | "Add DID <id> with <n> bytes, set up like DID <existing>." |
| Persistent data | "Add an NvM block for <data> with <n> bytes, like <existing block>." |
| A new customer delivery | "The new DBC is <file>. What changed, and what does it affect in the project?" |
| Parallel work | Merge two branches that changed the same ARXML files, then: "Resolve the ARXML conflicts." |
| Fix findings | "Fix the validation errors in <module>." |
| Review | "What changed in Com since the last commit?" |

| Task | Usual time | With the assistant | Result correct? | Notes |
|---|---|---|---|---|
| | | | | |

The target is half of the usual time on tasks like these. Tasks that need decisions only you can make (identifiers, timing, which example to copy) still need you: the assistant asks for them instead of inventing them.

## 5. What to report back

- The filled table.
- Every answer or tool message that was wrong or surprising, described in your own words, without project names or values.
- If you ran section 3: the answers to the questions in section 7 of [WORK-PC-CHECK.md](WORK-PC-CHECK.md).
- Conflicts that come back in every merge (for example dates or tool data in ADMIN-DATA): the merge can learn to settle those by itself.

## If something goes wrong

| What you see | What to do |
|---|---|
| No `ecu_*` tools in the chat | Run `ecuh doctor` in the project folder. If MCP servers are not allowed, use the terminal way from section 1 |
| "Cannot finish" | The reason is in the message. Ask the assistant to fix the findings it lists, or to discard the change: that restores the files exactly |
| "Files changed outside the harness" | Something else changed an ARXML file during the change, often the vendor tool's GUI saving. Decide: undo that edit, accept it (`ecuh change accept-external`), or discard the change |
| A finished change was wrong | `ecuh change undo <id>` restores its files, or use your version control |
| It is slow | The first answer on a large project waits until the files are read (seconds). Validation by the vendor tool takes as long as in the tool |

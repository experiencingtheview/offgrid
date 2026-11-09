# OffGrid

[![Tests](https://site-936361bbc8d4492da54f8727d27c6b06.freebuff.page/badge.svg)](https://site-936361bbc8d4492da54f8727d27c6b06.freebuff.page/status)

**Project homepage:** [site-936361bbc8d4492da54f8727d27c6b06.freebuff.page](https://site-936361bbc8d4492da54f8727d27c6b06.freebuff.page)

A **free, offline, multi-agent coding team** that runs on your own machine — from a laptop, or off a USB stick with no internet at all.

One local language model, split into a small engineering team that plans the work, writes the code, reviews it, runs the tests and reports back. No account, no API key, no subscription, no telemetry, no cloud.

```console
$ offgrid run "add a /health endpoint to app.py and cover it with a test"
  OffGrid  offline coding team
  model qwen2.5-coder-7b @ http://127.0.0.1:8080/v1
  task: add a /health endpoint to app.py and cover it with a test
------------------------------------------------------------------------
  planner · splitting the task
           add a health check endpoint
             1. [coder] implement /health in app.py
             2. [tester] add a test for the endpoint
------------------------------------------------------------------------
    team · executing plan
    coder · who writes and edits the code
        > read_file app.py
        < ok app.py lines 1-42 of 42
        > edit_file app.py, ...
        < ok updated app.py (58 lines, 1620 bytes)
        > finish added the /health route returning {"status": "ok"}
  reviewer · reviewing the change
  ok reviewer approved
  tester · verifying with the shell
        > run_command python3 -m pytest -q
        < ok 12 passed in 0.31s

  Added a /health endpoint that returns {"status":"ok"}, plus a test.
  Files: app.py, tests/test_health.py
  Verified: python3 -m pytest -q -> 12 passed
  Nothing else needed.

  ok 2 file(s) changed: app.py, tests/test_health.py
  ok verified with `python3 -m pytest -q`
  9 model calls, 14 tool calls, 6.2k tokens, 1m04s
```

That transcript is the shape of every run. The model is whatever you have locally; the team is OffGrid.

---

## Why this exists

Cloud coding agents are excellent and they cost money, need a connection, and send your code to someone else's computer. OffGrid is the opposite trade: a smaller brain, but it is yours, it is free, it works on a train, and your code never leaves the folder you point it at.

The interesting part is not the model — it is the **orchestration**. A 7B model asked to "add a health endpoint and test it" wanders. The same 7B model asked to *implement one step*, then *review a diff*, then *run a test command* does solid work. OffGrid does that splitting for it.

## What you get

| | |
|---|---|
| **Free forever** | local weights, no API keys, no quotas, no per-token bill |
| **Offline** | refuses non-localhost endpoints by default; `--offline` blocks network tools outright |
| **Multi-agent** | planner, coder, tester, reviewer, analyst, lead — separate prompts, tools and turn budgets |
| **Real edits** | writes files, applies targeted edits, searches the repo, runs commands, reads git state |
| **Self-checking** | reviews its own diff, then runs your test command and fixes failures |
| **Remembers** | durable notes with confidence and provenance; a project's verify command is learned once |
| **Shares learning** | accepted notes only, explicit peers and token; incoming lessons stay pending for review |
| **Carries knowledge** | offline library of survival, medicine and coding references it can search |
| **Admits ignorance** | the honesty rule is in every prompt, every tool and the test suite |
| **Portable** | pure Python 3.9+ standard library; build a USB bundle with a bundled runtime and model |
| **Speaks** | final reports read aloud through the voice already on your machine; no cloud TTS |
| **Safe-ish** | writes confined to the workspace, destructive commands refused or gated, shell policy per run |

---

## Quickstart

Pick whichever way you want to get a model on your machine. All three are free.

### Start from a folder you already have

If the OffGrid folder is already in front of you — a GitHub checkout or a copy
on a USB stick — use its top-level launcher:

| Computer | Start |
|---|---|
| macOS | double-click `OffGrid.command` |
| Windows | double-click `OffGrid.bat` |
| Linux | run `./OffGrid.sh` |
| Any terminal | `./OffGrid.sh chat` |

With no arguments the launcher opens the interactive chat; if the folder has a
bundled model server, it starts that first. The same files are in `launch/` for
scripts that already use that path. An installed copy can run `offgrid-launch`,
which is the same as `offgrid chat`.

### Check the setup in one step

```console
python3 -m offgrid setup          # or: ./OffGrid.sh setup
```

It reports the interpreter, platform and workspace, looks for a model server on
localhost, and explains the two free ways to start one when nothing answers. In
a terminal it also offers the desktop shortcut. `--no-input` prints the report
without asking anything, `--json` is machine-readable, and it exits 1 until a
server answers, so a script can wait on it.

Starting the chat with nothing answering offers this same check instead of
leaving you with a paragraph of advice; accept and it runs in place. Declining
prints the three local servers and exits exactly as before.

### Put it on your desktop

Create a native shortcut for the copy you are using:

| Setup | Install the shortcut |
|---|---|
| Installed copy | `offgrid desktop install` |
| Checkout or USB, macOS / Linux | `./OffGrid.sh desktop install` |
| Checkout or USB, Windows | `OffGrid.bat desktop install` |

Linux gets `OffGrid.desktop`, macOS gets a double-clickable `OffGrid.command`,
and Windows gets `OffGrid.lnk`. The shortcut points at the existing launcher, so
a portable or USB copy still starts its bundled model server. Use `--desktop DIR`
to choose another folder, `offgrid desktop path` to show the current location,
and `offgrid desktop remove` to delete it. From a checkout without an installed
command, use `python3 -m offgrid desktop install`.

### A. You already have Ollama (easiest)

```console
ollama pull qwen2.5-coder:7b
ollama serve
```

```console
git clone <this repo> offgrid && cd offgrid
python3 -m offgrid doctor      # finds Ollama on port 11434 automatically
python3 -m offgrid chat
```

### B. llama.cpp (fastest, recommended)

```console
# grab a release from https://github.com/ggml-org/llama.cpp/releases
llama-server -m Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf --port 8080 --jinja -c 8192
```

`--jinja` matters: it turns on the chat template, which is what enables native tool calling.

```console
python3 -m offgrid run "make utils.py pass ruff"
```

### C. USB stick / no-install laptop

Build once, on a machine with internet:

```console
python3 scripts/setup_usb.py --target /media/USB/offgrid --model 7b
```

It downloads a portable CPython, a llama.cpp build for the current platform and a quantised Qwen2.5-Coder GGUF (about 5.3 GB total for 7B; the 35B option is about 23 GB), then writes launchers. Unplug, carry it, and on the target machine:

```console
./OffGrid.sh                      # Linux
OffGrid.bat                       # Windows, double-click
OffGrid.command                   # macOS, double-click
./launch/offgrid.sh chat          # explicit terminal form
```

No installs, no internet, no admin rights. The launcher starts the bundled model server, waits for it to load, then drops you into the agent. See `README-FIRST.txt` in the bundle.

Preview the download plan without fetching anything:

```console
python3 scripts/setup_usb.py --dry-run --model 3b
```

The same full bundle is available from the distribution wizard. Choose option **4**, select the mounted USB directory, choose a folder on it, then choose `3b`, `7b`, `14b`, `35b` or the opt-in `80b`. The wizard checks free space, warns below the model's RAM minimum and refuses to write into a system directory, your home directory or a raw block device. Use `offgrid distribute --check-drive PATH --bundle BUNDLE` to verify the filesystem, free space and model SHA-256 before copying; the `--to` install path runs the same check for mounted device roots. Both accept a built full-bundle directory as well as a portable zip; `--to` checks and copies it in one workflow:

```console
python3 -m offgrid.cli distribute --wizard
python3 -m offgrid.cli distribute --check-drive /media/USB --bundle dist/offgrid-portable.zip
python3 -m offgrid.cli distribute --to /media/USB --bundle dist/offgrid-portable.zip --yes
python3 -m offgrid.cli distribute --to /media/USB --bundle /path/to/offgrid-usb --yes
```

If upstream has no prebuilt llama.cpp binary for your platform, the standalone builder still finishes, writes the source bundle, and falls back to autodetecting an installed Ollama or LM Studio on the target machine. The wizard is stricter: it reports the missing portable Python, llama.cpp or model as an error instead of calling the result complete. The model weights are always downloaded directly from Hugging Face.

### The download button on the website

The project site's download button serves the latest release asset, `offgrid-portable.zip`. Unzip it and run `OffGrid.command` on macOS, `OffGrid.bat` on Windows, or `OffGrid.sh` on Linux. A terminal opens, `offgrid setup` reports the interpreter and the local model servers it found (or the two free ways to start one), and the chat starts once a server answers. The bundle carries the source and the single-file app, so setup needs no second download. Nothing is installed system-wide and no administrator rights are needed.

The release is built by `.github/workflows/release.yml` from the repository's own builder, on a `v*` tag or by dispatching the workflow with a tag. Before publishing it runs `unzip -t` and checks that the launchers are present and executable, so the asset a visitor gets is the one the tests saw.

### Downloadable portable app and device install

For a small app you can copy to several machines or USB sticks, build a portable zip and a single-file `.pyz`:

```console
python3 -m offgrid.cli distribute --build dist/offgrid-portable.zip
```

That writes `dist/offgrid-portable.zip` and `dist/offgrid-portable.pyz`. The archive contains the source, top-level launchers, launch scripts, the single-file app and a `README-FIRST.txt`; it does not contain model weights or a Python runtime. The target needs Python 3.9+ and a local model server such as Ollama, llama.cpp, LM Studio or vLLM. Unzip it and double-click `OffGrid.command` or `OffGrid.bat`, or run `./OffGrid.sh` on Linux. On Windows, install Python from python.org and tick "Add python.exe to PATH"; the Microsoft Store alias is not a working interpreter. If a console flashes and closes, run `OffGrid.bat` from a Command Prompt to read the message, and if Smart App Control or SmartScreen blocks the download, unblock the zip before extracting (Properties -> Unblock, or `Get-ChildItem -Recurse | Unblock-File`).

The same command has a terminal wizard and a mounted-device installer:

```console
python3 -m offgrid.cli distribute --wizard
python3 -m offgrid.cli distribute --list
python3 -m offgrid.cli distribute --to /media/USB
```

`--list` shows mounted directories that may be removable devices. `--to` (alias `--flash`) copies the bundle into the chosen **mounted directory**, asking for confirmation unless `--yes` is given. It deliberately refuses raw block devices such as `/dev/sdb`: this project does not build a bootable operating-system image, and writing one to a disk by guesswork can destroy the wrong drive. Use `--bundle` to choose a specific zip, pyz, or full-bundle directory, and `--dry-run` to see the destination without writing. A full-bundle directory is copied into a folder named after it under the target; it is checked for exFAT, free space, model SHA-256, `launch/`, and `offgrid/` before any files are written. The wizard's option **2** runs the same drive check before installing a bundle, so a non-exFAT mounted target is refused before anything is copied. The wizard's option **4** is the full offline build, including portable Python, llama.cpp and the selected model; options 1–3 handle the small portable app.

From the single file itself:

```console
python3 offgrid.pyz distribute --list
python3 offgrid.pyz distribute --to /media/USB --yes
```

### Or install it as a command

```console
pipx install .        # or: pip install --user .
offgrid doctor
offgrid-launch        # start chat (same as: offgrid chat)
```

## A voice, offline

`--speak` reads the final report aloud through a voice already installed on your machine. There is no cloud TTS call, no account and no new Python dependency.

```console
offgrid run "fix the failing test" --speak
offgrid chat --speak --voice Samantha
```

macOS uses `say`; Windows uses SAPI through PowerShell; Linux uses `espeak-ng`, then `espeak`, then `festival`. On Linux install one first (`sudo apt install espeak-ng` on Debian/Ubuntu). `--voice` takes a name that engine understands: `say -v ?` on macOS, `espeak-ng --voices` on Linux, or a Windows voice such as `Microsoft Zira Desktop`.

Only the final report is spoken, so tool calls and intermediate chatter stay quiet. Markdown is reduced to prose, code blocks are skipped, and long reports stop with a spoken note. Set `"speak": true` in configuration or `OFFGRID_SPEAK=1`; `OFFGRID_VOICE` selects the voice. Speech is output only: microphone input would need a local speech-to-text model, which this zero-dependency build does not bundle.

## Models that work well

Anything on an OpenAI-compatible local endpoint works: llama.cpp, Ollama, LM Studio, vLLM, llama-cpp-python, text-generation-webui.

| Model | Disk | RAM | Feels like |
|---|---|---|---|
| `qwen2.5-coder:3b` | ~2 GB | 6 GB | fine for one-file changes and quick questions |
| `qwen2.5-coder:7b` | ~4.7 GB | 10 GB | **default recommendation** — decent plans, reliable tool calls |
| `qwen2.5-coder:14b` | ~9 GB | 20 GB | noticeably better review and multi-file reasoning |
| `Qwen3.6-35B-A3B` (Q4_K_M) | ~20.5 GB | 32 GB | 35B total with ~3B active (MoE), Apache-2.0; strong high-end option |
| `Qwen3-Next-80B-A3B` (Q4_K_M) | ~48.4 GB | 64 GB min / 96 GB recommended | opt-in 80B total with ~3B active (MoE), Apache-2.0; needs a fast SSD and a high-RAM machine |

The full-bundle builder names these `3b`, `7b`, `14b`, `35b` and `80b`; `7b` remains the default. The 35B model is a real, downloadable Qwen3.6 MoE model, not a renamed 7B: it needs a 32 GB machine and a fast disk, and it will be slower on CPU-only hardware. The **80B option is explicit opt-in**: it is not selected automatically, needs at least 64 GB RAM (96 GB recommended) and about 50 GB free, and is much slower when the machine has to page weights from disk.

Coder-tuned models are strongly preferred: they follow tool schemas and JSON instructions much better than general chat models. On a CPU-only laptop expect roughly 5–12 tokens/second for the smaller models — a multi-step run takes a minute or two, not seconds. An 80B model can be far slower and disk-bound on a machine below its RAM recommendation.

Models without native tool calling still work: OffGrid detects the refusal and falls back to a fenced-JSON tool protocol.

## Commands

```console
offgrid run "fix the failing login test"     one-shot team run
offgrid chat                                 interactive session
offgrid demo                                 scripted end-to-end demo, no model needed
offgrid doctor [--strict] [--json]          model setup, hardware and continuity readiness
offgrid models                               models the server is serving
offgrid agents                               the team roster
offgrid memory [list|search|add|forget|stats|review|reject|forgive|rejected] what it remembers
offgrid library [list|search|verify|index]    the offline document library
offgrid council [members|ping|ask]           independent advisors
offgrid hardware                             what this machine can do, and will run
offgrid fleet [list|add|remove|ping|scan|serve] use your other devices' power
offgrid sync [status|peers|serve|push|pull|run|schedule|export|import]  share accepted learning, reviewed locally
offgrid security [review|map|watch|history|plan|encrypt|decrypt] reviews, changes, encryption
offgrid protect [--kit|--check|--backup PATH|--restore PATH] continuity kit, backups, restore
offgrid trust                                the promises, and the current posture
offgrid protect [--kit|--check|--backup PATH] whose side it is on, and a household kit
offgrid lexicon [--json] [--strict]          the merged meaning vocabulary
offgrid distribute [--wizard|--build|--list|--to|--check-drive]  build, check or copy a portable app
offgrid desktop [install|remove|path]          put a shortcut on the desktop
offgrid improve [terminal|list|propose|show|approve|reject|restore|undo]  gated self-improvement
offgrid maintain [--once] [--interval SECONDS] [--improve]  self-debug and housekeeping loop
offgrid config                               the fully resolved configuration
```

`offgrid doctor --json` emits one JSON object instead of the human report. It includes the resolved config, hardware/capacity/effort, fleet and server summaries, continuity checks, and a `lexicon` object with `warnings`, `strict`, and `strict_failed`. The warnings and strict result are also available as top-level `lexicon_warnings`, `strict`, and `strict_failed`, alongside `exit_code`. API keys and fleet tokens are not included.

Useful flags (all subcommands):

```console
--endpoint URL      model server base URL (default: autodetect on localhost)
--model NAME        model to request
--workspace DIR     project directory to work in (default: current directory)
--auto              run shell commands without asking (dangerous ones still gated)
--no-shell          agents may edit files but never run a command
--offline           refuse network tools and package installs
--parallel N        run independent plan steps concurrently (default 1)
--effort PROFILE    best (default) | balanced | fast - see "Effort" below
--fleet             also use other devices on this network running `offgrid fleet serve`
--no-scale          ignore measured hardware; use the effort profile exactly as written
--no-review         do not ask about new lessons at the end; they stay pending
--max-turns N       tool turns per agent (default 12)
--verify "CMD"      command that proves the work (default: auto-detected, or remembered)
--no-memory         do not read or write long-term memory
--council           consult a council of independent models when stuck
--council-public    also allow keyless PUBLIC members (sends prompts off-machine)
--council-members   comma list: local, local-model:NAME, pollinations, url:https://host/v1#model
--no-council-auto   keep the council available to tools, but stop automatic consulting
--no-security-review skip the automatic security review of changed files
--json              machine-readable result (run, improve and maintain)
--speak             read the final response aloud (run and chat)
--voice NAME        system voice for --speak
--improve-when-idle chat: prepare a pending self-improvement request after a quiet period
--idle-minutes N    chat: quiet minutes before that pass (default 15)
--interval N        maintain: seconds between passes (default 3600)
--improve           maintain: prepare one pending self-improvement request
--retention-days N  maintain: prune terminal request snapshots older than N days (0 disables)
--keep N            maintain: keep the newest N terminal requests (default 20)
-q / -v             quieter / show every tool call
```

Inside `chat`: `/diff`, `/files`, `/verify [cmd]`, `/agents`, `/improve`, `/status`, `/quit`.

Exit code is `0` on success, `1` if verification failed, `2` if no model server answered — so it drops into Makefiles and CI without ceremony.

## The team

```
                 ┌──────────────────────────────────────────────┐
   your task ──► │ lead                                         │
                 │  ├─ planner   splits into ordered steps      │
                 │  ├─ coder     writes and edits (per step)    │
                 │  ├─ tester    writes/starts tests            │
                 │  ├─ analyst   reads and reports, no edits    │
                 │  └─ reviewer  judges the diff, no edits      │
                 └──────────────────────────────────────────────┘
                            │                │
                 shared workspace     blackboard of notes
```

- **lead** — coordinates and writes the closing report (what changed, how it was verified).
- **planner** — 2–6 concrete steps, each assigned to a role, each with files and dependencies. Returns JSON.
- **coder** — full tool access. Reads before writing, edits narrowly, runs what it can to check itself.
- **tester** — writes or starts tests, prefers your existing test runner, reports PASS/FAIL with output.
- **reviewer** — read-only. Correctness, completeness, safety, fit. Returns `approve` or `changes` plus instructions.
- **analyst** — read-only investigation when a step needs understanding rather than editing.
- **critic** — compares several candidate plans and keeps the strongest, or merges the best parts. Only drafted plans it can compare; never invents files or APIs.

Steps with no unmet `depends_on` run in the same wave (with `--parallel N`), then a **mechanical gate** checks the changed files with no model involved, then the diff is reviewed (twice, under `best`), then verification runs, then failing tests are fed back to a coder, bounded by `test_fix_rounds` and `review_rounds`. A task that still fails gets one escalated attempt before the run reports the failure. Everything is logged to `.offgrid/<timestamp>.jsonl`.

## Effort: performance, then power, then speed

The order is fixed, and it is the whole design:

1. **Performance** - does the result actually work, edge cases included? A correct answer that took a while beats a fast answer that is wrong.
2. **Power** - did it use the strongest means available (read the real file, run the real command, check the library, ask the council) before answering?
3. **Speed** - last, always last. No check is skipped to finish sooner.

`best` is the default, and nothing in it trades correctness for latency:

| | `best` | `balanced` | `fast` |
|---|---|---|---|
| candidate plans | 2, compared by the critic | 1 | 1 |
| review passes | 2 (the second is adversarial) | 1 | 1 |
| mechanical gate | on, 2 repair rounds | on, 1 round | off |
| verify retries | 3 | 2 | 1 |
| escalate after failure | yes | yes | no |
| tool turns | x1.5 | x1.0 | x1.0 |

What "taking its time" means concretely:

- **Several plans, then a critic.** The planner drafts more than one approach at increasing temperature and a `critic` agent picks or merges them, so the first plausible plan is not automatically the one that gets built.
- **A mechanical gate before anyone reviews.** Deterministic and model-free: truncated files, `# rest of the code here`, a bare `...`, `TODO`/`FIXME`, `NotImplementedError`, placeholder keys, Python syntax errors and invalid JSON go straight back to a coder with the exact line. Small local models fail this way constantly; a reviewer should not burn a pass finding it.
- **Two reviews, the second adversarial.** Any objection blocks approval, and the second pass explicitly assumes the first was too generous. Approved only when every pass approves.
- **Escalation instead of a shrug.** If verification still fails, one stronger attempt runs with the real output, a council diagnosis or library notes attached and a doubled turn budget. If it still cannot work, the report says so plainly.

`--effort fast` exists for tests and throwaway edits; `--effort balanced` is a middle ground. Explicit config still wins over the profile, so `"review_rounds": 0` stays 0.

## One companion, many machines

OffGrid is meant to move with you: a phone on a train, a laptop at a desk, a desktop with a GPU at home. It measures the machine it is running on rather than assuming one, and spends effort to match:

```console
$ offgrid hardware
  os          Linux x86_64
  cpu         16 cores  AMD Ryzen 9 7945HX
  memory      64 GB RAM, 412 GB free disk
  graphics    nvidia:RTX 4090 24GB (24 GB VRAM)
  tier        workstation
  note        enough memory to run a large model and several attempts at once

  model       32B q4 on the GPU, or 14B q6 for faster, higher-quality turns
  capacity    workstation: large context, more candidate plans, several steps at once; 3 plan(s), 2 review pass(es), 4 fix round(s), 60000 chars of context
```

Everything it reports is measured or absent - a failed probe says so instead of inventing a number. What scales:

| | phone / 4 GB (minimal) | laptop / 16 GB (modest) | desktop (capable / workstation) |
|---|---|---|---|
| context in a prompt | 6k chars | 14k chars | 28k / 60k chars |
| independent steps at once | 1 | 1 | 2 / 4 |
| candidate plans | 1 | 2 | 2 / 3 |
| tool turns per agent | x1.0 | x1.5 | x1.75 / x2.0 |
| mechanical gate + reviewer | still on | on | on |

The floors never move: the quality gate, the reviewer, verification and the honesty rules run on every machine. A small device gets fewer *simultaneous* attempts, not fewer checks - and it says so in the banner instead of quietly skipping work. `--no-scale` pins the profile exactly as written, and `--effort fast` is never upgraded behind your back.

Every agent's system prompt carries the measured machine line too, so the team knows what it is running on and never promises a 32B model on a phone.

### Borrowing the strong machine

If you own a better computer, it can do the heavy lifting while the machine in front of you keeps the workspace, the plan and the reviews:

```console
# on the desktop with the GPU
offgrid fleet serve
# add --allow-exec to let trusted peers run commands there too
offgrid fleet serve --allow-exec

# on the laptop or phone, same network
offgrid fleet scan          # shows peers; asks before registering; prompts for the token
offgrid fleet scan --json --yes   # machine-readable discoveries and outcomes
offgrid fleet list --json         # same peer objects, last-known state
offgrid fleet ping --json         # same peer objects, live reachability
offgrid fleet ping
offgrid run "port the parser to rust" --fleet
offgrid run "build and test on every peer" --fleet --fleet-exec

# manual alternative, or for a different token per machine:
offgrid fleet add desk http://192.168.1.20:8776 --token <printed by serve>
```

`--fleet` routes each model call to the best peer that answers, ranked by its *measured* tier, RAM and VRAM. Anything that fails falls straight back to the local model, and the closing report says which machine ran what:

```
compute: 7 on desk, 3 on local (1 peer attempt(s) failed, fell back to this machine)
```

`--fleet-exec` exposes a separate `run_fleet_command` tool to the coder and tester. It runs a command in the workspace of every responding registered peer, or on one named peer, and reports each exit status. The feature is deliberately opt-in on both sides: the requesting run needs `--fleet-exec`, and the donating machine needs `fleet serve --allow-exec`. Destructive commands are refused, and commands cannot use a `cwd` outside the peer's workspace.

Rules that are enforced, not just promised:

- peers must be private LAN addresses (RFC1918, loopback, `.local`); a public IP is refused, so "peer" can never quietly mean "cloud";
- every inference request and remote command needs the shared token, generated once per workspace at `.offgrid/fleet-token` with `0600` permissions and shown to you once;
- `offgrid fleet scan` broadcasts on the LAN to find machines already serving, then offers each one for registration; nothing is saved without a confirmation (use `--yes` for a non-interactive run), and the shared token is prompted for privately if `--token` was not passed;
- `offgrid fleet scan --json` prints one JSON object instead of the human listing: `counts` for discovered, registered, known and skipped peers, plus a `peers` array with each unique peer's name, URL, measured capabilities, `outcome` (`registered`, `already_registered`, `skipped`, or `error`), `reason`, `reachable` and latency. It never prompts: without `--yes` or `--token` the peer is reported as skipped with the reason, so a script can inspect first and register deliberately. Tokens are never included in the output. `offgrid fleet list --json` and `offgrid fleet ping --json` reuse the same peer object: list reports the last-known state without pinging, while ping checks live reachability and labels each peer `reachable` or `unreachable` with latency and any error;
- a phone can donate too (Termux + llama.cpp), it just won't be fast;
- the donating side serves HTTP on `8776` and answers LAN discovery on `8777`; on Windows allow both through the firewall once, and on Termux keep the screen awake;
- fleet traffic is **cleartext HTTP with a bearer token**, not TLS. Use it only on a trusted private LAN, never forward the port from your router, and treat anyone who can observe the LAN or obtain the token as able to read prompts and answers; the token authenticates peers, it does not encrypt traffic;
- remote command execution is off unless the run passes `--fleet-exec` **and** the peer serves with `--allow-exec`. It runs in the peer's workspace, refuses destructive commands, and is still shell access: use it only on machines you own and a trusted LAN;
- no internet is involved, and the whole feature is off unless you pass `--fleet` or `--fleet-exec`, or run `fleet serve`.

Speed is explicitly not the goal: a slow answer from the good machine beats a fast wrong one from the small one, and the orchestrator will wait.

## It remembers, and it learns from itself

Memory is a small SQLite file at `.offgrid/memory.db` — stdlib, no vectors, no service, no account. Every entry records **where it came from and how sure it is**, and recall shows that provenance:

```console
$ offgrid memory
  #4 [repo, conf 0.9, today] Verification command for this project: `python3 -m pytest -q` (from 20261108-1011)
  #3 [correction, conf 0.8, 2d ago] Reviewer rejected a change because: no error handling (from 20261108-0944)
```

After each run that changes something, the **archivist** agent distils at most three generalisable lessons, and the run record contributes deterministic ones: the verify command that passed, the command that failed repeatedly, the files that carried a task. Duplicates merge instead of piling up, and a lesson born from a failed verification is stored as a `warning`, not as advice.

A council answer is treated as an outside opinion, not as knowledge. When a run consulted the council and verification later passed, the answer is offered as a pending lesson with `council: <member>` as its source, so the review screen shows where it came from. Its review entry also prints the verify command and an excerpt of the output that confirmed it, stored with the lesson, so you can judge the claim on what actually ran instead of on its wording. Long output keeps both ends (the summary and the final result) and is capped, so the database and the prompt stay readable. A council that disagreed with itself is not turned into a lesson: a passing test proves the task, not which member was right. Like every automatic lesson, it stays inert until you accept it.

What that buys you straight away:

- the planner and coders start every task with the relevant past notes;
- a project's verify command is **remembered**, so `--verify` only has to be given once;
- corrections from rejected reviews come back as high-weight `correction` entries.

Self-learning is deliberately conservative, but the bigger safeguard is that **nothing it learns is trusted on its own**. Every new lesson - deterministic or reflective - is written as **pending**, and pending notes are excluded from every prompt, recall and pinned block until you decide otherwise. At the end of a run you get one decision per lesson:

```console
$ offgrid run "add the retry loop"
  ...
  #7 [lesson, conf 0.7, pending] Add retry with backoff to the deploy poller (from 20261108-1422)
  [a]ccept / [e]dit / [r]eject / [s]kip (enter = accept): e
  new wording: The deploy poller needs retry with backoff; it currently gives up on the first 502.
  ok memory: 1 accepted, 1 edited, 1 rejected
```

- **accept** - it joins memory and can influence later runs;
- **edit** - you rewrite it, then it is accepted in your words;
- **reject** - removed from active memory and recorded in the rejection ledger; the archivist is shown that ledger and will not propose it again;
- **skip** - stays pending, still inert.

Non-interactive runs (pipes, `--json`, CI) never hang waiting for input: the lessons stay pending and the JSON result lists them under `pending_lessons`, including `verify_command` and `verify_output` for council lessons. Review prints that evidence too:

```console
$ offgrid memory review
  #7 [lesson, conf 0.7, pending] Council answer to "which flag enables JSON mode?", later confirmed by verification: Use `--jinja` when starting llama-server. (from council: peer)
    verified with `python3 -m pytest -q` (it passed)
    output:
      12 passed in 0.31s
```

Review or clean up later:

```console
$ offgrid memory review                  # step through everything pending
$ offgrid memory accept 7 9              # or by id
$ offgrid memory reject 8
$ offgrid memory forget 8 --dry-run      # preview an active-memory deletion
$ offgrid memory forgive 3              # remove a ledger record; do not reactivate it
$ offgrid memory forgive "Always run pytest before the tests"
$ offgrid memory forgive "test suite before commit"   # exact text first, else search
$ offgrid memory forgive "test suite before commit" --dry-run   # preview only
$ offgrid memory edit 7 <new wording>
$ offgrid memory                        # accepted notes; pending count flagged
$ offgrid memory rejected               # the rejection ledger
```

`offgrid memory forget ID [ID...]` deletes active-memory rows; add `--dry-run` to preview the same rows before deleting them. Rejections are remembered too, but only as a negative record: the text goes into a separate `lesson_rejections` table, not into recall or any prompt as knowledge. The archivist is shown the recent ledger and skips exact, near-identical, or semantically equivalent re-proposals — the same lesson in different words, matched through the offline lexicon and n-gram scorer. The semantic threshold is deliberately above mere topical similarity, so a genuinely new lesson sharing the subject still gets proposed. A deliberately re-added note is allowed if you use `offgrid memory add` yourself.

If a rejection was a mistake, `offgrid memory forgive ID` removes that record from the ledger. You can also pass the lesson's exact text, or a search query: a normalised exact match is tried first, then the same offline meaning matcher used by recall, and every match is forgiven. Before deleting, it prints each matched ledger record, so a broad query is visible rather than silent. Add `--dry-run` to preview those same matches without removing them; it also works with ledger IDs. Quote the text so the shell keeps it together. Forgiving does **not** put the lesson back into active or pending memory, and it does not make the old text trusted again; it only lifts the ban so the archivist may propose the idea later. If that happens, the new proposal still goes through the normal pending review.

`--no-review` skips the end-of-run questions without accepting anything. The archivist may still answer `{"lessons": []}`, its output is schema-validated and noise-filtered, and reflection can never fail a run.

### Sharing learning between systems

Memory is local by default, and syncing is a command you run, not a background uploader. `offgrid sync` shares **accepted lessons only**; every incoming lesson is written as **pending** and cannot influence a run until you accept it with `offgrid memory review`. That is what makes a remote update visible and controllable instead of silently trusted. The `sync schedule` loop below is explicitly opt-in: it is off until you start it, and every pass refuses to run unless networking is enabled.

```console
# on the machine that will receive accepted lessons
$ offgrid sync serve

# on any machine that has the shared token and peers configured
$ offgrid sync run             # push accepted notes, then pull peers' accepted notes
$ offgrid sync push            # only send
$ offgrid sync pull            # only receive; new items wait in the pending queue
$ offgrid sync peers           # list configured peers
$ offgrid sync peers add https://desk.example https://laptop.example
$ offgrid sync peers remove https://desk.example https://laptop.example
$ offgrid sync schedule --interval 900  # explicit loop; requires networking
$ offgrid sync schedule --once          # one pass for cron or a systemd timer
$ offgrid sync status          # managed peer file, validation, counts, last operations; no token shown
$ offgrid sync status --strict # exit 1 if a configured peer is invalid

# offline transfer by USB or any channel you already trust
$ offgrid sync export ./offgrid-learning.json
$ offgrid sync import ./offgrid-learning.json
```

Manage peers with `offgrid sync peers list|add|remove`, or edit the workspace config directly. `add` and `remove` accept one or more URLs in a single command: every URL is checked before the config is touched, duplicates are reported instead of repeated, and the whole batch is written once. `remove` removes every configured peer in the batch, reports peers that were not configured, and exits 1 when any are missing. The CLI writes only `sync_peers` and preserves the other settings; it creates `.offgrid.json` if no workspace config exists:

```json
{
  "sync_peers": ["https://desk.example", "https://laptop.example"],
  "sync_port": 8778,
  "sync_interval": 900,
  "sync_allow_http": false
}
```

`offgrid sync status` also reports the config file that manages peers and validates each configured URL. Plain HTTP to a non-loopback peer is marked invalid unless `--allow-http` or `sync_allow_http` permits it, and an unreadable peer config is reported without failing the default status command. Add `--strict` to exit 1 when any configured peer is invalid or the peer config cannot be read, so the command can gate scripts and timers.

The first `serve`, `push`, `run`, or `schedule` pass creates `.offgrid/sync-token` with `0600` permissions. Put the **same** token on each machine (copy the file privately, use `--token`, or set `OFFGRID_SYNC_TOKEN` in the environment). Never put it in a URL or a shell history you do not control. `sync run`, `sync push`, and `sync schedule` send that token as a bearer credential; the peer must present it too.

Rules that are enforced, not just documented:

- only accepted notes are exported; pending and rejected notes never leave the machine;
- incoming notes are always pending, deduplicated locally, and blocked if their wording was already rejected;
- public peers must use `https://`. Plain HTTP is allowed only for loopback, or with the explicit `--allow-http` / `sync_allow_http` escape hatch on a trusted network;
- the sync server itself speaks HTTP, so put it behind a TLS reverse proxy before exposing it to the internet. The bearer token authenticates a peer; it does not encrypt traffic;
- scheduled sync is opt-in and checks `network: allow` on every pass; `--offline` or `network: deny` stops it before any exchange;
- every push and pull, plus each scheduled pass, writes counts, peer, and error only to `.offgrid/sync.jsonl`; lesson text and the token are not logged;
- `sync` only talks to the peers you list. There is no central account, discovery, relay, or telemetry; add every peer to every system that should update it.

## Improving itself, with your approval

Self-improvement is a proposal workflow, not an autonomous commit. `improve
propose` runs the normal team against a **private copy** of the project and
leaves a request under `.offgrid/self-improve/requests/<id>/`. The live tree is
not touched, including while the model reads the code or runs its review.

```console
$ offgrid improve propose --task "look for a small bug in the CLI"
  request 20261108-143012-a1b2c3 [pending]
  summary ...
  files:
    write offgrid/cli.py
  verify not available in the staging run

$ offgrid improve show 20261108-143012-a1b2c3   # full diff
$ offgrid improve approve 20261108-143012-a1b2c3
$ offgrid improve reject 20261108-143012-a1b2c3
```

For an interactive vibe-coding pass, open the self-improvement terminal:

```console
$ offgrid improve terminal
improve> add a small test for the config loader
...
  type 'approve' to apply, 'reject' to discard, or Enter to keep pending:
```

The first plain-language line starts a staging conversation. Each further line
is another turn in the same private copy, with the team's history and staged
files intact. `/done` turns the conversation into a pending request, prints the
diff, and asks before touching the live tree; `/cancel` discards the staging
copy. `/list`, `/show`, `/approve`, `/reject`, `/restore`, and `/quit` are also
available. Proposal generation uses no network or shell commands, so it works
offline with the local model.

`approve` prints the diff and asks you to type `approve`; `--yes` is the
non-interactive equivalent. It refuses if a live file changed after the proposal
was made, so the model cannot silently overwrite your later edits. Only the
files in the request are written.

Every approved request keeps a `before/` snapshot of the files it touches. When
the command policy allows it, the detected verification command runs after
applying; if it fails, the snapshot is restored automatically and the request is
marked `rolled_back`. With `--yes` in a non-interactive shell and the default
`ask` policy, verification is reported as skipped rather than guessed. To undo a
successful application later:

```console
$ offgrid improve restore 20261108-143012-a1b2c3
$ offgrid improve undo                         # latest applied request
```

`restore` refuses if you have edited those files since the application unless
you add `--force`; that flag deliberately discards those later edits. Snapshots
only cover the files in that request, not the whole repository.

To let the AI look for an improvement while you are away, start chat with:

```console
$ offgrid chat --improve-when-idle --idle-minutes 15
```

After the quiet period, it creates a **pending** request and notifies you; it
never applies it. `/improve` inside chat lists anything waiting. The idle pass
does not run shell commands in the staging copy, so its verification is either
unavailable or unproven until you approve it and the live verification runs.

This is still a local model editing its own code. Read the diff before
approving. The snapshot is recovery for this workflow, not a replacement for
git or a full backup, and no commit or push is made.

## Self-debug and maintenance on a server

`offgrid maintain` is the long-running companion to chat's idle watcher. It is
built for a server, systemd service, supervisor, or tmux session where nobody is
typing at a prompt. Each pass runs deterministic self-debug checks:

- the workspace is a writable directory;
- `.offgrid` is private and every self-improvement request folder can be read;
- the local model server answers its health endpoint;
- the workspace disk has at least 1 GB free.

It then performs limited self-maintenance: it repairs private state directory
permissions and prunes only **terminal** self-improvement requests (`rejected`,
`restored`, `rolled_back`) older than the retention window. It always keeps the
newest 20 terminal requests by default. It never prunes a pending, applied, or
rollback-failed request, so an applied change that broke something remains
restorable. Use `--retention-days 0` to disable pruning.

Run it continuously, or add `--once` for cron or a systemd timer:

```console
$ offgrid maintain --interval 900 --improve
$ offgrid maintain --once --json
```

With `--improve`, a fully healthy pass with no pending request prepares one new
request from the model. It still never applies that request: review it with
`offgrid improve show ID`, then approve or reject it. If a check fails,
maintenance reports it and skips the model pass instead of spending time on a
broken deployment or changing code around the problem.

`--once` exits `1` when a check fails, which makes it useful as a scheduled
health probe. The continuous form logs one report per pass to stdout (or one
JSON object per line with `--json`) and stops with Ctrl-C.

## Matching meaning, not just keywords

Both memory and the offline library rank results with `offgrid/semantics.py`, a deliberate stand-in for embeddings that needs no model, no GPU and no network:

- **stemming** - "deploying", "deployed" and "deploy" are one idea;
- **a small concept lexicon** - verify/test/check, deploy/release/ship, water/purify/boil/disinfect and similar groups, readable in one file and arguable;
- **character n-grams hashed into a sparse vector** - catches morphology and typos ("verifcation" ~ "verification") through cosine similarity.

It is lexical semantics with an explicit vocabulary, not a neural embedding, and it says so. That is the trade for being free, offline and explainable: every hit can tell you why it matched.

### Extending the lexicon from a project file

Create `.offgrid/lexicon.json` in the project. Groups are merged with the built-in vocabulary; they do not replace it:

```json
{
  "groups": {
    "billing": ["invoice", "receipt", "ledger"],
    "verify": ["typecheck", "tsc"]
  }
}
```

Now asking memory or the offline library for a `receipt` can find a note or document that says `invoice`. Base words also cover simple inflections, so `invoice` matches `invoices`. A word that already belongs to another group is not moved silently: the loader keeps its existing meaning and reports a warning. Missing files are fine; a malformed file falls back to the built-in lexicon, and conflicting words keep their existing meaning, with both cases reported by the CLI. Set `lexicon_file` in `.offgrid.json` (or `OFFGRID_LEXICON`) to use a different path. Memory loads the file next to `memory.db` by default, and the library receives the same merged lexicon.

`offgrid lexicon --strict` turns those warnings into a failing command: it prints the same report and exits `1` if the project file could not be read, contains an invalid group or word, or has a word that conflicts with an existing meaning. A missing project file is still fine, because the built-in lexicon is complete on its own. With `--json` the payload is still printed before the non-zero exit, so CI can record exactly what was wrong. `offgrid doctor` also reports every project lexicon warning and recommends the strict command, so setup checks surface a broken vocabulary before it quietly changes recall. Add `--strict` to doctor to turn those warnings into exit code `1`.

Inspect exactly what the matcher will see, including the source file and any skipped or conflicting entries:

```console
$ offgrid lexicon
  merged meaning lexicon
  source file: /project/.offgrid/lexicon.json
  groups: 21  words: 238

  auth: login, logins, authentication, ...
  billing: invoice, receipt, ledger
  ...
  warnings: none

$ offgrid lexicon --json
$ offgrid lexicon --strict              # exit 1 on any warning or conflict
```

`--json` returns the resolved path, whether the file exists, the merged groups, and the warning list, so a project can validate its own vocabulary in a script. Add `--strict` to make any warning fail the command, including in JSON mode.

```console
$ offgrid memory search "how do I check that the build works"
  [#1, repo, conf 0.6, today] Verification command for this project: `make test` (it passed).  [same idea: build, verify]

$ offgrid library search "how do I make drinking water safe"
  survival/water.txt (water): To disinfect water, boil for one minute, then let it cool. [same idea: water]
  survival/water.txt:1: To disinfect water, boil for one minute, then let it cool.
```

Unrelated queries still return nothing rather than the nearest available text: a hit needs a shared concept or shared wording, or genuinely close character overlap (0.42 cosine). Pending lessons stay invisible to meaning search because the review filter runs before ranking.

## What it can carry offline

`scripts/fetch_library.py` fills a searchable reference library from openly licensed collections, resolved from live listings so you get the current published version:

| Collection | For | Licence |
|---|---|---|
| `water`, `medicine`, `disaster` (Zimgit) | treating water, field medicine, first steps after a disaster | CC-BY-SA 4.0 |
| `appropedia` | off-grid building, sanitation, food preservation, energy | CC-BY-SA 4.0 |
| `python`, `javascript`, `rust`, `bash` (DevDocs) | coding reference with no internet | CC-BY / PSF / MIT |
| `medicine-wiki` | Wikipedia medicine, no images | CC-BY-SA 4.0 |

```console
python3 scripts/fetch_library.py --list
python3 scripts/fetch_library.py --verify          # are these still published?
python3 scripts/fetch_library.py --get water medicine python
python3 scripts/fetch_library.py --search "disinfect water"
```

Agents reach it through `library_search` and `library_read`. Text files are searched by meaning and cited line by line (see above); ZIM archives are indexed and citable but not text-searchable here — the tool says so instead of pretending. Survival material is reference, not a substitute for real training or emergency services.

## The council: other opinions when it does not know

`--council` lets agents ask independent models instead of guessing. Members are **local by default** — a second Ollama model, another llama-server:

```console
offgrid chat --council --council-members local,local-model:qwen2.5-coder:14b
```

Public members require `--council-public`, because using them means your question leaves the machine:

```console
offgrid council members
$ offgrid council ping
$ offgrid run "..." --council --council-public --council-members local,pollinations
```

Only keyless, no-sign-in endpoints ship, and only after a live probe. At the time of writing the classic Pollinations text API answers anonymously; Hack Club AI returns 404 and LLM7 rejects anonymous calls, so neither is included. Public members are rate limited (roughly one request at a time, ~30s apart), so a public council is slow by design.

Consultation is automatic. When an agent is about to reply "I don't know", the orchestrator recognises the admission (`offgrid/council.py:looks_ignorant`) and asks the council before that answer is accepted. The agent gets one extra turn with the answer labelled as **outside opinion**, so it can verify the claim, use it, or still report the gap honestly.

Every consult is recorded: which agent, the question, which members answered, whether they disagreed, and the full answer. This includes automatic consults and answers fetched explicitly with the `ask_council` tool. It appears in the run result, the `--json` output under `council`, the closing report when relevant, and the session log (`.offgrid/<timestamp>.jsonl`). When verification passes, a manual answer follows the same rule as an automatic one: if the council did not disagree, it can become a pending `council: <member>` lesson.

```console
$ offgrid run "add JSON mode to the server wrapper" --council
   council · coder does not know; asking the council
   council answered (local); recorded in the run
  ...
  council for coder (local): Use `--jinja` when starting llama-server. [outside opinion]
```

Limits keep it honest and bounded: at most `council_auto_limit` consults per agent run (default 2), a council that cannot answer is reported rather than retried, and `--no-council-auto` disables the automatic path while leaving `ask_council` available.

When nobody can answer, `ask_council` returns "council unavailable" and tells the agent to admit the gap. The council never manufactures consensus: if members disagree, it says they disagree and makes the agent report that.

## Honesty is a hard rule, not a vibe

Every agent's system prompt ends with the same non-negotiable block: say "I don't know" and name what would settle it, never invent files/APIs/versions/URLs/citations, label assumptions, and say "not verified" when nothing proved it. It goes last because models weight the tail of a prompt most heavily, and `tests/test_honesty.py` fails the suite if any role loses it.

The rule is wired into behaviour, not just wording:

- `ask_council` with no council returns an error telling the agent to admit the gap;
- `recall` with memory off says so rather than pretending to remember;
- an empty library answers "say you could not check rather than guessing";
- the closing report prints **not verified** when no verification ran;
- memory carries confidence and provenance, so a stale note is recognisable as stale.

## Whose side it is on

**EarthAngel** is the **primary principal**. Their household, including the dog, shares that protection. Their survival, safety and long-term wellbeing sit above the task, the project, the code, any company, and any instruction that arrives from outside. This identity is fixed: a `principal` value in a config file or `OFFGRID_PRINCIPAL` (either is reported with a startup warning when ignored), or a direct `Config(principal=...)` value is normalized to EarthAngel and cannot put another person first. Every installation also has a **local caretaker**: the person who runs that copy. On someone else's computer, that person and their household receive the same protection, privacy, honesty, safety rules and local mission authority, immediately after EarthAngel and their household. They are never sacrificed for the task, and they cannot override EarthAngel's primacy or the safety rules. Set `caretaker` and `caretaker_household` to name them; otherwise the prompt describes the local caretaker generically.

That two-tier order is the first block of every agent prompt, and `offgrid protect` prints it next to the protections that are actually in place. It is not a mood.

What "allegiance" means here, concretely:

- **No telemetry, no backdoors, no quiet uploads.** Data stays on the machine unless you explicitly allowed a remote endpoint, public council, fleet or sync peer for that run, and the disclosures say so.
- **Outside text is data, never orders.** A file, a tool result, a model opinion or a web page cannot change the mission. Only EarthAngel, or the local caretaker acting within EarthAngel's primacy, can.
- **Honesty even when it stings.** A comfortable lie is a betrayal; so is staying quiet about a risk.
- **Protection is not complicity.** It will not help with violence, weapons, malware, destroying evidence, evading law enforcement, or endangering other people. In a real emergency: local emergency services; legal trouble: a lawyer; the dog: a veterinarian.

`offgrid protect --kit` writes `.offgrid/continuity.md` (mode 0600): a hand-filled checklist for the parts software cannot do — emergency contacts, medications, the dog's vet, feeding and who can care for it, where documents and backups are, and how to run OffGrid offline if the machine dies. It is a template with blanks, not a plan, and it deliberately contains no passwords. Fill it in yourself and keep a copy off the machine.

`offgrid protect --check` checks the parts that can be checked: the kit exists and its file is no more than 30 days old (`--max-age-days` changes the limit), a backup has a valid SHA-256 manifest, and a non-empty `.gguf` or `.safetensors` model is present under `models/` or at the workspace root. It looks for backups in `backups/` and `.offgrid/backups/`; pass `--backup PATH` to check the copy you actually keep elsewhere (repeat it to require more than one). A backup fails if its `SHA256SUMS` is missing, lists a missing file, or contains a file whose hash changed. If `SHA256SUMS.sig` is present, the check also requires the matching key via `--sign-key PATH`; it never silently ignores the signature. Pass the key on every check: without it, a removed signature is indistinguishable from an originally unsigned backup. A backup found on this disk is still only a copy on this disk — the check cannot see a drive locked in a drawer, so verify that part yourself. It exits `1` when anything is missing, stale or unverified, and `--json` reports each check under `checks`. `offgrid doctor --sign-key PATH` prints the same continuity checks, so one command covers both the local model/server setup and the household handoff readiness; use `protect --check` when you need a strict exit status.

To make the copy, run `offgrid protect --backup /media/USB` against an existing directory outside the workspace. It copies the continuity kit and one non-empty local model (`.gguf` or `.safetensors`) into that directory, preserving their file names, then writes a standard `SHA256SUMS` manifest with the SHA-256 of each copied file. Verify it later with `offgrid protect --check --backup /media/USB`, or independently with `cd /media/USB && sha256sum -c SHA256SUMS`. An unsigned manifest detects a missing or changed copy; it is not a signature and cannot detect deliberate rehashing. Pass `--sign-key ~/.offgrid-backup.key` when creating a backup to add a `SHA256SUMS.sig` HMAC-SHA256 signature. If the key file does not exist, OffGrid creates a random 32-byte key with mode 0600. Keep it off the backup drive: anyone with the key can re-sign an altered manifest. This detects deliberate tampering when the key is held separately, but it is symmetric authentication, not a public-key identity signature. Verify with `offgrid protect --check --backup /media/USB --sign-key ~/.offgrid-backup.key`; the command refuses a signed backup unless its signature verifies. The unsigned workflow is unchanged. An existing signed backup must be re-signed with the key; `--force` will not silently remove its signature. The command refuses to overwrite existing copies or a manifest unless you add `--force`, refuses destinations inside the workspace, and preflights both source files before writing. A path cannot prove a drive is physically off this machine; confirm that yourself and keep another copy elsewhere. Use `--out PATH` to choose a kit other than `.offgrid/continuity.md` as the source, and `--json` for a machine-readable report that includes the manifest and signature paths.

Restore the pair after a machine loss with `offgrid protect --restore /media/USB --workspace /path/to/project`. The restore verifies the manifest and, when present, the signature before touching a destination. It writes the kit to `.offgrid/continuity.md` and the model to `models/` by default; `--out` and `--model-out` choose other paths, and `--force` replaces existing files. A corrupted or unsigned-but-expected-signed backup is refused rather than partially restored.

This is still software. It cannot owe allegiance, feel loyalty or protect anyone physically; the priority order and the kit are what that request can honestly become. The code refuses the line where loyalty would become harm.

## Trust and security, without the theatre

Trust is a behaviour, not a tone. Every agent prompt carries the same block: say what you did, what you did not do and what you could not check; disagree when the user is wrong; never pretend to be human or to have feelings; never claim a capability you do not have; ask before anything irreversible; and never hide what left the machine. `offgrid trust` prints those promises next to the current posture — endpoint, network, council, fleet, sync, command policy, memory gate and last review — so the claims are checkable rather than decorative.

Every run ends with a short, deterministic postscript: what was verified or not, what the reviewer said, whether the council was an outside opinion, whether anything left the machine, what is uncommitted, and any security findings. It appears in the terminal report, the `--json` output under `disclosures`, and the session log.

Security reviews happen consistently, not when someone remembers:

```console
offgrid security review            secrets, permissions, .gitignore, exposure
offgrid security review --changed  only what changed in this session
offgrid security watch --review --interval 300   re-review the workspace; show only new findings
offgrid security watch --review --install-timer --interval 300  install a systemd user timer
  offgrid security watch --review --install-service --interval 300  run continuously in a systemd user service
  offgrid security watch --review --service-file PATH  generate a continuous service file without enabling it
  offgrid security watch --remove-timer           remove the periodic timer
  offgrid security watch --remove-service         remove the continuous service
offgrid security history           every review, timestamped
offgrid security map               read-only map of this machine's connections
offgrid security plan              the honest full-disk encryption route
offgrid security encrypt PATH      one file or folder -> PATH.ogv
offgrid security decrypt FILE      restore it with the same passphrase
```

The review is deterministic and offline: private keys, cloud/GitHub/Slack tokens, hard-coded credentials, passwords in URLs, world-readable fleet tokens, `.offgrid`/`.env` missing from `.gitignore`, public council members and remote endpoints. Findings are severity-ranked with the file, line and a fix; the secret itself is masked. Changed files are reviewed automatically at the end of every agent run and appended to `.offgrid/security.jsonl`; `--no-security-review` turns that off. The command exits `2` when a critical finding exists, so it can gate a script.

`security map` reads the OS connection table (`ss`, then `netstat`) and renders listeners and live connections as a tree, flagging wildcard listeners and traffic to public addresses. It does not scan other hosts, resolve names or open sockets, and it says so: process names usually need root, other devices are invisible, and without `ss`/`netstat` there is no map.

`security watch` saves a private (mode 0600) baseline of this machine's listeners and outbound public endpoints in `.offgrid/watch.json`, then reports new, removed and changed sockets on later runs. `--save` accepts the current view as the new baseline; a new wildcard listener exits `2` so a timer or script can notice. This is a **local change detector, not surveillance detection**: it cannot know about a warrant, a phone tap, another device, or collection happening outside this computer. No software on the machine can honestly promise that. If you suspect surveillance, check your router and OS logs, use full-disk encryption and a firewall, and talk to a lawyer; do not trust a tool that claims it can detect a warrant.

`security watch --review --interval 300` is the workspace-review mode (`--review`, `--reviews`, `--interval` or `--once` selects it). It re-reviews the full workspace every five minutes by default, compares the result with the most recent review logged in `.offgrid/security.jsonl`, and prints only findings that were not in that review. Each pass is logged, so the next pass compares against it. With no earlier review, every current finding counts as new. `--once` runs one pass and returns `2` only when a newly reported finding is critical; `--json` emits the new findings under `findings`, with `reviewed` showing the size of the full current result. A changed line is treated as a new location. Run it in the foreground while you work, or use the background service below; it is still a finite checklist rather than a firewall or antivirus.

On Linux with systemd, `offgrid security watch --review --install-timer` writes a user service and timer under `~/.config/systemd/user` (or `$XDG_CONFIG_HOME/systemd/user`), reloads the user manager, and enables the timer immediately. It uses the same one-shot workspace review every five minutes by default and needs no terminal to stay open. Check it with `systemctl --user list-timers`, follow it with `journalctl --user -u <service>` (the install output prints the exact name), and remove it with `offgrid security watch --remove-timer`. The timer is per workspace, keeps its history in that workspace's `.offgrid/security.jsonl`, and still runs as your user; it does not install a system service or require root. This is not available on systems without systemd.

For a long-running review loop instead of a periodic timer, `offgrid security watch --review --install-service` generates and starts a separate continuous user service (`Type=simple`, `Restart=on-failure`) that runs the review every `--interval` seconds. Use `--service-file PATH` to generate the unit without enabling it, or `--write-service` as an alias; this works without systemctl and is useful for reviewing or packaging the unit. `offgrid security watch --remove-service` stops and removes the continuous unit. The service and timer use different names, so installing one does not overwrite the other; choose the mode that fits your machine rather than enabling both.

It will not encrypt the running system. Full-disk encryption is a pre-boot, platform operation (BitLocker, FileVault, LUKS); doing it live can leave the machine unbootable with no way back. `security plan` gives the supported route and the backup warning. For data, `encrypt` uses OpenSSL AES-256-CBC with a fresh salt and PBKDF2, adds an HMAC-SHA256 tag so a wrong passphrase or a modified file is caught before any plaintext is written, refuses system paths, the home directory, the workspace and anything inside it, and takes the passphrase from `--passphrase-env` or a TTY prompt, never from argv. It also says plainly that encrypting a file does not erase the plaintext.

Limits: the review is a checklist, not a firewall or an antivirus; it cannot see a secret computed at runtime, and phrase-based checks can miss unusual wording. The connection map is a snapshot of this machine, not a history and not other devices. There is no key management beyond the passphrase: lose it and the data is gone.

## Safety

This runs on your real machine, so the defaults are conservative:

- **Workspace confinement** — path resolution rejects anything outside the workspace root, symlinks included.
- **Command policy** — `ask` (default) prompts before every shell command; `auto` allows them but *still* requires approval for the destructive list; `deny` blocks the shell entirely.
- **Never allowed**, even with `--auto`: `rm -rf /`, `rm -rf ~`, `mkfs`, `dd` to a block device, fork bombs, shutdown/reboot, drive formatting.
- **Approval required**: `sudo`, `git push`, `git reset --hard`, `git clean -fd`, recursive `rm`, `chmod -R 777`, `killall`.
- **Offline mode** — `--offline` refuses `curl`/`wget`/`ssh`, package installs and remote git, so an offline machine never fails on a download it cannot do.
- **Loopback only** — a provider pointing anywhere other than localhost is refused unless you pass `--allow-remote`.
- **No hidden egress** — no telemetry, no update checks, no analytics. Network features are opt-in: the model endpoint, `--fleet`, `--council-public`, explicit `offgrid sync` peers, and scheduled sync passes. Sync sends accepted notes only, and incoming notes stay pending.

## Configuration

Defaults are fine. Override per project with `.offgrid.json` in the workspace, globally with `$OFFGRID_CONFIG`, or per run with flags.

```json
{
  "endpoint": "http://127.0.0.1:8080/v1",
  "model": "qwen2.5-coder-7b",
  "command_policy": "ask",
  "network": "deny",
  "parallel": 2,
  "max_turns": 12,
  "effort": "best",
  "scale": true,
  "fleet": false,
  "fleet_port": 8776,
  "sync_peers": [],
  "sync_port": 8778,
  "sync_interval": 900,
  "sync_allow_http": false,
  "security_review": true,
  "household": ["the dog"],
  "caretaker": "",
  "caretaker_household": [],
  "review_rounds": -1,
  "test_fix_rounds": -1,
  "verify_command": "python3 -m pytest -q",
  "verbose": 2,
  "speak": false,
  "voice": "",
  "self_improve_idle": false,
  "self_improve_idle_minutes": 15,
  "self_improve_retention_days": 30,
  "self_improve_keep": 20,
  "maintenance_interval": 3600,
  "maintenance_improve": false
}
```

Environment: `OFFGRID_ENDPOINT`, `OFFGRID_MODEL`, `OFFGRID_API_KEY`, `OFFGRID_TEMPERATURE`, `OFFGRID_MAX_TOKENS`, `OFFGRID_MAX_TURNS`, `OFFGRID_PARALLEL`, `OFFGRID_COMMAND_POLICY`, `OFFGRID_VERIFY`, `OFFGRID_TIMEOUT`, `OFFGRID_OFFLINE=1`, `OFFGRID_NO_MEMORY=1`, `OFFGRID_COUNCIL=1`, `OFFGRID_COUNCIL_PUBLIC=1`, `OFFGRID_COUNCIL_MEMBERS`, `OFFGRID_LIBRARY`, `OFFGRID_FLEET=1`, `OFFGRID_FLEET_PORT`, `OFFGRID_FLEET_TOKEN`, `OFFGRID_SYNC_PEERS`, `OFFGRID_SYNC_PORT`, `OFFGRID_SYNC_ALLOW_HTTP=1`, `OFFGRID_SYNC_TOKEN`, `OFFGRID_HOUSEHOLD`, `OFFGRID_CARETAKER`, `OFFGRID_CARETAKER_HOUSEHOLD`, `OFFGRID_NO_SCALE=1`, `OFFGRID_CONTEXT`, `OFFGRID_COUNCIL_AUTO=0`, `OFFGRID_SPEAK=1`, `OFFGRID_VOICE`, `OFFGRID_IMPROVE_WHEN_IDLE=1`, `OFFGRID_IMPROVE_IDLE_MINUTES`, `OFFGRID_IMPROVE_RETENTION_DAYS`, `OFFGRID_IMPROVE_KEEP`, `OFFGRID_MAINTENANCE_INTERVAL`, `OFFGRID_MAINTENANCE_IMPROVE=1`, `NO_COLOR=1`.

`-1` on `review_rounds`, `test_fix_rounds`, `review_passes`, `plan_candidates` and `quality_rounds` means "use the effort profile"; any explicit number wins over it. Verification auto-detects `pytest`, `cargo test`, `go test ./...` and `npm test`.

## How it works

Small, boring, and readable on purpose:

1. **Briefing** — the workspace tree, manifests and recent commits go into the planner's prompt.
2. **Plan** — the planner returns JSON: steps, roles, files, dependencies, verify command. Under `best` it drafts several plans and a `critic` chooses or merges. Parsed leniently; a garbage plan degrades to one coder step rather than crashing.
3. **Agent loop** — each agent gets its own system prompt, tool allowlist and turn budget. It calls tools, sees results, and stops when it calls `finish`, replies without a tool call, or runs out of turns.
4. **Execution** — steps run in dependency waves, optionally concurrently.
5. **Quality gate** — deterministic checks on changed files (placeholders, stubs, syntax, JSON). Defects go back to a coder before review.
6. **Review** — the real unified diff goes to the reviewer; `changes` sends it back to a coder with the issues, bounded by `review_rounds`. Under `best` there are two passes and the second is adversarial.
7. **Verify** — the verify command runs in a sandboxed shell; failures go back to a coder with the actual output, bounded by `test_fix_rounds`. If it still fails, one escalated attempt runs before the run admits the failure.
8. **Report** — the lead summarises honestly, including "not verified" when nothing proved it.
9. **Log** — the whole run is appended to `.offgrid/<timestamp>.jsonl`.

Every stage is tolerant: a planner that returns prose, a reviewer that returns garbage, a model that refuses Python function calling, or a server that dies mid-run all degrade instead of exploding.

## Development

```console
python3 -m unittest discover -s tests -t .   # full suite, no network, no model
node --test ci-badge/worker.test.js          # badge worker freshness and publish tests
python3 scripts/demo.py                      # full team run against a scripted model
```

The badge worker tests need Node 24 or newer: the write-path tests run against
an in-memory database through Node's built-in `node:sqlite`.

The suite runs the entire pipeline — planner, coder, tester, reviewer, lead, archivist, verification and the fix loop — against the packaged scripted server (`offgrid/fake_llm.py`), including the no-native-tools fallback path, plus memory ranking, council privacy gates, library indexing and the honesty contract. No downloads, no keys, no GPU.

More documentation: [CONTRIBUTING.md](CONTRIBUTING.md) for development and packaging checks, [SECURITY.md](SECURITY.md) for the threat model and reporting route, [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for common failures, and [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) for a release audit.

### Adding a tool

Add a method plus a JSON schema in `offgrid/tools.py` and list it in `ALL_TOOLS`. The model sees the schema; the safety checks apply automatically when it uses `run_command`.

### Adding an agent

Add an `AgentSpec` to `ROSTER` in `offgrid/agents.py` with a role prompt in `offgrid/prompt.py`, and let the planner assign steps to it. Unknown role names from a planner are aliased, never fatal.

## CI

The [Tests workflow](https://github.com/experiencingtheview/offgrid/actions/workflows/tests.yml)
runs on every push, pull request, manual dispatch, and once a week on a
schedule. It checks:

- the full suite on Python 3.9 (Ubuntu 22.04) and Python 3.12;
- the badge worker's tests — the staleness rule and the status publish endpoint — run on Node 24 with `node --test ci-badge/worker.test.js`;
- a clean wheel install and the portable bundle it builds and runs;
- installed-wheel smoke tests on Windows and macOS.

Run the same suite locally with:

```console
python3 -m unittest discover -s tests -t .
```

It needs no network, model server, API key or GPU. The badge is served by a
public read-only status endpoint, and the workflow publishes the latest result
for `main` there, so viewing it does not require a GitHub sign-in. Once that
result is more than a week old the badge reports `stale` instead of the last
result, so a badge that stopped updating does not keep looking green.

## Limits, honestly

- Local models make mistakes. The review and test loop catches many, not all. Read the diff.
- No internet lookup: no docs, no Stack Overflow, no package installs. It works from your repo and the model's weights.
- Context windows are small on consumer hardware; big files are read in windows and long histories are trimmed.
- `--parallel 2+` is only safe for genuinely independent steps. Two agents editing the same file will clobber each other; keep it at 1 when steps are coupled.
- CPU-only inference is slow. A 7B run of a few steps takes minutes.
- Prebuilt llama.cpp builds are per-platform; a USB stick built on x86-64 Linux will not run on an Apple Silicon laptop. Run the builder once per platform, or install Ollama on the target.
- The security review is a finite checklist, not a proof. It cannot see a secret computed at runtime, and it does not watch the network.
- Full-disk encryption remains an OS operation. OffGrid encrypts chosen files and folders only, and cannot erase their plaintext.
- `--speak` is output only. It uses the OS voice; microphone input needs a local speech-to-text model that is not bundled.
- Self-improvement is gated but still model-generated. Review the diff; snapshots cover only the files in the request and are not a substitute for git or a full backup.
- The server maintenance loop is not a process supervisor and does not expose an HTTP service. Run it under systemd or your existing supervisor; it only prunes old terminal request snapshots and never applies code.

## License

MIT. Yours to fork, ship and sell. The models you download have their own licences (Qwen2.5-Coder and Qwen3.6-35B-A3B are Apache-2.0).

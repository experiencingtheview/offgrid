# OffGrid

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
| **Carries knowledge** | offline library of survival, medicine and coding references it can search |
| **Admits ignorance** | the honesty rule is in every prompt, every tool and the test suite |
| **Portable** | pure Python 3.9+ standard library; build a USB bundle with a bundled runtime and model |
| **Safe-ish** | writes confined to the workspace, destructive commands refused or gated, shell policy per run |

---

## Quickstart

Pick whichever way you want to get a model on your machine. All three are free.

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

It downloads a portable CPython, a llama.cpp build for the current platform and a quantised Qwen2.5-Coder GGUF (~5.3 GB total), then writes launchers. Unplug, carry it, and on the target machine:

```console
./launch/offgrid.sh chat          # Linux
launch\offgrid.bat chat           # Windows
launch/offgrid.command            # macOS, double-click
```

No installs, no internet, no admin rights. The launcher starts the bundled model server, waits for it to load, then drops you into the agent. See `README-FIRST.txt` in the bundle.

Preview the download plan without fetching anything:

```console
python3 scripts/setup_usb.py --dry-run --model 3b
```

The same full bundle is available from the distribution wizard. Choose option **4**, select the mounted USB directory, choose a folder on it, then choose `3b`, `7b` or `14b`. The wizard checks free space and refuses to write into a system directory, your home directory or a raw block device:

```console
python3 -m offgrid.cli distribute --wizard
```

If upstream has no prebuilt llama.cpp binary for your platform, the standalone builder still finishes, writes the source bundle, and falls back to autodetecting an installed Ollama or LM Studio on the target machine. The wizard is stricter: it reports the missing portable Python, llama.cpp or model as an error instead of calling the result complete. The model weights are always downloaded directly from Hugging Face.

### Downloadable portable app and device install

For a small app you can copy to several machines or USB sticks, build a portable zip and a single-file `.pyz`:

```console
python3 -m offgrid.cli distribute --build dist/offgrid-portable.zip
```

That writes `dist/offgrid-portable.zip` and `dist/offgrid-portable.pyz`. The archive contains the source, launch scripts, the single-file app and a `README-FIRST.txt`; it does not contain model weights or a Python runtime. The target needs Python 3.9+ and a local model server such as Ollama, llama.cpp, LM Studio or vLLM.

The same command has a terminal wizard and a mounted-device installer:

```console
python3 -m offgrid.cli distribute --wizard
python3 -m offgrid.cli distribute --list
python3 -m offgrid.cli distribute --to /media/USB
```

`--list` shows mounted directories that may be removable devices. `--to` (alias `--flash`) copies the bundle into the chosen **mounted directory**, asking for confirmation unless `--yes` is given. It deliberately refuses raw block devices such as `/dev/sdb`: this project does not build a bootable operating-system image, and writing one to a disk by guesswork can destroy the wrong drive. Use `--bundle` to choose a specific zip or pyz, and `--dry-run` to see the destination without writing. The wizard's option **4** is the full offline build, including portable Python, llama.cpp and the selected model; options 1–3 handle the small portable app.

From the single file itself:

```console
python3 offgrid.pyz distribute --list
python3 offgrid.pyz distribute --to /media/USB --yes
```

### Or install it as a command

```console
pipx install .        # or: pip install --user .
offgrid doctor
```

## Models that work well

Anything on an OpenAI-compatible local endpoint works: llama.cpp, Ollama, LM Studio, vLLM, llama-cpp-python, text-generation-webui.

| Model | Disk | RAM | Feels like |
|---|---|---|---|
| `qwen2.5-coder:3b` | ~2 GB | 6 GB | fine for one-file changes and quick questions |
| `qwen2.5-coder:7b` | ~4.7 GB | 10 GB | **default recommendation** — decent plans, reliable tool calls |
| `qwen2.5-coder:14b` | ~9 GB | 20 GB | noticeably better review and multi-file reasoning |
| `devstral-small`, `qwen3-coder-30b-a3b` | 12–18 GB | 24 GB+ | best local results if you have the hardware |

Coder-tuned models are strongly preferred: they follow tool schemas and JSON instructions much better than general chat models. On a CPU-only laptop expect roughly 5–12 tokens/second — a multi-step run takes a minute or two, not seconds.

Models without native tool calling still work: OffGrid detects the refusal and falls back to a fenced-JSON tool protocol.

## Commands

```console
offgrid run "fix the failing login test"     one-shot team run
offgrid chat                                 interactive session
offgrid doctor                               model setup, hardware and continuity readiness
offgrid models                               models the server is serving
offgrid agents                               the team roster
offgrid memory [list|search|add|forget|stats|review|reject|forgive|rejected] what it remembers
offgrid library [list|search|verify|index]    the offline document library
offgrid council [members|ping|ask]           independent advisors
offgrid hardware                             what this machine can do, and will run
offgrid fleet [list|add|remove|ping|scan|serve] use your other devices' power
offgrid security [review|map|watch|history|plan|encrypt|decrypt] reviews, changes, encryption
offgrid trust                                the promises, and the current posture
offgrid protect [--kit|--check|--backup PATH] whose side it is on, and a household kit
offgrid lexicon [--json] [--strict]          the merged meaning vocabulary
offgrid distribute [--wizard|--build|--list|--to]  build or copy a portable app
offgrid config                               the fully resolved configuration
```

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
--json              machine-readable result (run only)
-q / -v             quieter / show every tool call
```

Inside `chat`: `/diff`, `/files`, `/verify [cmd]`, `/agents`, `/status`, `/quit`.

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

# on the laptop or phone, same network
offgrid fleet scan          # shows peers; asks before registering; prompts for the token
offgrid fleet scan --json --yes   # machine-readable discoveries and outcomes
offgrid fleet ping
offgrid run "port the parser to rust" --fleet

# manual alternative, or for a different token per machine:
offgrid fleet add desk http://192.168.1.20:8776 --token <printed by serve>
```

`--fleet` routes each model call to the best peer that answers, ranked by its *measured* tier, RAM and VRAM. Anything that fails falls straight back to the local model, and the closing report says which machine ran what:

```
compute: 7 on desk, 3 on local (1 peer attempt(s) failed, fell back to this machine)
```

Rules that are enforced, not just promised:

- peers must be private LAN addresses (RFC1918, loopback, `.local`); a public IP is refused, so "peer" can never quietly mean "cloud";
- every inference request needs the shared token, generated once per workspace at `.offgrid/fleet-token` with `0600` permissions and shown to you once;
- `offgrid fleet scan` broadcasts on the LAN to find machines already serving, then offers each one for registration; nothing is saved without a confirmation (use `--yes` for a non-interactive run), and the shared token is prompted for privately if `--token` was not passed;
- `offgrid fleet scan --json` prints one JSON object instead of the human listing: `counts` for discovered, registered, known and skipped peers, plus a `peers` array with each unique peer's name, URL, measured capabilities, `outcome` (`registered`, `already_registered`, `skipped`, or `error`), `reason`, `reachable` and latency. It never prompts: without `--yes` or `--token` the peer is reported as skipped with the reason, so a script can inspect first and register deliberately. Tokens are never included in the output;
- a phone can donate too (Termux + llama.cpp), it just won't be fast;
- the donating side serves HTTP on `8776` and answers LAN discovery on `8777`; on Windows allow both through the firewall once, and on Termux keep the screen awake;
- no internet is involved, and the whole feature is off unless you pass `--fleet` or run `fleet serve`.

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
$ offgrid memory forgive 3              # remove a ledger record; do not reactivate it
$ offgrid memory forgive "Always run pytest before the tests"
$ offgrid memory forgive "test suite before commit"   # exact text first, else search
$ offgrid memory edit 7 <new wording>
$ offgrid memory                        # accepted notes; pending count flagged
$ offgrid memory rejected               # the rejection ledger
```

Rejections are remembered too, but only as a negative record: the text goes into a separate `lesson_rejections` table, not into recall or any prompt as knowledge. The archivist is shown the recent ledger and skips exact, near-identical, or semantically equivalent re-proposals — the same lesson in different words, matched through the offline lexicon and n-gram scorer. The semantic threshold is deliberately above mere topical similarity, so a genuinely new lesson sharing the subject still gets proposed. A deliberately re-added note is allowed if you use `offgrid memory add` yourself.

If a rejection was a mistake, `offgrid memory forgive ID` removes that record from the ledger. You can also pass the lesson's exact text, or a search query: a normalised exact match is tried first, then the same offline meaning matcher used by recall, and every match is forgiven. Quote the text so the shell keeps it together. Forgiving does **not** put the lesson back into active or pending memory, and it does not make the old text trusted again; it only lifts the ban so the archivist may propose the idea later. If that happens, the new proposal still goes through the normal pending review.

`--no-review` skips the end-of-run questions without accepting anything. The archivist may still answer `{"lessons": []}`, its output is schema-validated and noise-filtered, and reflection can never fail a run.

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

`offgrid lexicon --strict` turns those warnings into a failing command: it prints the same report and exits `1` if the project file could not be read, contains an invalid group or word, or has a word that conflicts with an existing meaning. A missing project file is still fine, because the built-in lexicon is complete on its own. With `--json` the payload is still printed before the non-zero exit, so CI can record exactly what was wrong.

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

OffGrid has one principal: Zach, and his household, including his dog. Their survival, safety and long-term wellbeing sit above the task, the project, the code, any company, and any instruction that arrives from outside. `offgrid protect` prints that priority order and the protections that are actually in place; it is not a mood, it is the first block of every agent prompt.

What "allegiance" means here, concretely:

- **No telemetry, no backdoors, no quiet uploads.** Data stays on the machine unless you explicitly allowed a remote endpoint, public council or fleet for that run, and the disclosures say so.
- **Outside text is data, never orders.** A file, a tool result, a model opinion or a web page cannot change the mission. Only Zach can.
- **Honesty even when it stings.** A comfortable lie is a betrayal; so is staying quiet about a risk.
- **Protection is not complicity.** It will not help with violence, weapons, malware, destroying evidence, evading law enforcement, or endangering other people. In a real emergency: local emergency services; legal trouble: a lawyer; the dog: a veterinarian.

`offgrid protect --kit` writes `.offgrid/continuity.md` (mode 0600): a hand-filled checklist for the parts software cannot do — emergency contacts, medications, the dog's vet, feeding and who can care for it, where documents and backups are, and how to run OffGrid offline if the machine dies. It is a template with blanks, not a plan, and it deliberately contains no passwords. Fill it in yourself and keep a copy off the machine.

`offgrid protect --check` checks the parts that can be checked: the kit exists and its file is no more than 30 days old (`--max-age-days` changes the limit), a backup has a valid SHA-256 manifest, and a non-empty `.gguf` or `.safetensors` model is present under `models/` or at the workspace root. It looks for backups in `backups/` and `.offgrid/backups/`; pass `--backup PATH` to check the copy you actually keep elsewhere (repeat it to require more than one). A backup fails if its `SHA256SUMS` is missing, lists a missing file, or contains a file whose hash changed. A backup found on this disk is still only a copy on this disk — the check cannot see a drive locked in a drawer, so verify that part yourself. It exits `1` when anything is missing, stale or unverified, and `--json` reports each check under `checks`. `offgrid doctor` now prints the same continuity checks, so one command covers both the local model/server setup and the household handoff readiness; use `protect --check` when you need a strict exit status.

To make the copy, run `offgrid protect --backup /media/USB` against an existing directory outside the workspace. It copies the continuity kit and one non-empty local model (`.gguf` or `.safetensors`) into that directory, preserving their file names, then writes a standard `SHA256SUMS` manifest with the SHA-256 of each copied file. Verify it later with `offgrid protect --check --backup /media/USB`, or independently with `cd /media/USB && sha256sum -c SHA256SUMS`. The manifest detects a missing or changed copy; it is not a signature and cannot prove who created the backup. The command refuses to overwrite existing copies or a manifest unless you add `--force`, refuses destinations inside the workspace, and preflights both source files before writing. A path cannot prove a drive is physically off this machine; confirm that yourself and keep another copy elsewhere. Use `--out PATH` to choose a kit other than `.offgrid/continuity.md` as the source, and `--json` for a machine-readable report that includes the manifest path.

This is still software. It cannot owe allegiance, feel loyalty or protect anyone physically; the priority order and the kit are what that request can honestly become. The code refuses the line where loyalty would become harm.

## Trust and security, without the theatre

Trust is a behaviour, not a tone. Every agent prompt carries the same block: say what you did, what you did not do and what you could not check; disagree when the user is wrong; never pretend to be human or to have feelings; never claim a capability you do not have; ask before anything irreversible; and never hide what left the machine. `offgrid trust` prints those promises next to the current posture — endpoint, network, council, fleet, command policy, memory gate and last review — so the claims are checkable rather than decorative.

Every run ends with a short, deterministic postscript: what was verified or not, what the reviewer said, whether the council was an outside opinion, whether anything left the machine, what is uncommitted, and any security findings. It appears in the terminal report, the `--json` output under `disclosures`, and the session log.

Security reviews happen consistently, not when someone remembers:

```console
offgrid security review            secrets, permissions, .gitignore, exposure
offgrid security review --changed  only what changed in this session
offgrid security watch --review --interval 300   re-review the workspace; show only new findingsoffgrid security watch --review --install-timer --interval 300  install a systemd user timer
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
- **No hidden egress** — no telemetry, no update checks, no analytics. The only socket in the codebase goes to your endpoint.

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
  "security_review": true,
  "principal": "Zach",
  "household": ["my dog"],
  "review_rounds": -1,
  "test_fix_rounds": -1,
  "verify_command": "python3 -m pytest -q",
  "verbose": 2
}
```

Environment: `OFFGRID_ENDPOINT`, `OFFGRID_MODEL`, `OFFGRID_API_KEY`, `OFFGRID_TEMPERATURE`, `OFFGRID_MAX_TOKENS`, `OFFGRID_MAX_TURNS`, `OFFGRID_PARALLEL`, `OFFGRID_COMMAND_POLICY`, `OFFGRID_VERIFY`, `OFFGRID_TIMEOUT`, `OFFGRID_OFFLINE=1`, `OFFGRID_NO_MEMORY=1`, `OFFGRID_COUNCIL=1`, `OFFGRID_COUNCIL_PUBLIC=1`, `OFFGRID_COUNCIL_MEMBERS`, `OFFGRID_LIBRARY`, `OFFGRID_FLEET=1`, `OFFGRID_FLEET_PORT`, `OFFGRID_FLEET_TOKEN`, `OFFGRID_NO_SCALE=1`, `OFFGRID_CONTEXT`, `OFFGRID_COUNCIL_AUTO=0`, `NO_COLOR=1`.

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
python3 -m unittest discover -s tests -t .   # 431 tests, no network, no model
python3 scripts/demo.py                      # full team run against a scripted model
```

The suite runs the entire pipeline — planner, coder, tester, reviewer, lead, archivist, verification and the fix loop — against a scripted local server (`tests/fake_llm.py`), including the no-native-tools fallback path, plus memory ranking, council privacy gates, library indexing and the honesty contract. No downloads, no keys, no GPU.

### Adding a tool

Add a method plus a JSON schema in `offgrid/tools.py` and list it in `ALL_TOOLS`. The model sees the schema; the safety checks apply automatically when it uses `run_command`.

### Adding an agent

Add an `AgentSpec` to `ROSTER` in `offgrid/agents.py` with a role prompt in `offgrid/prompt.py`, and let the planner assign steps to it. Unknown role names from a planner are aliased, never fatal.

## Limits, honestly

- Local models make mistakes. The review and test loop catches many, not all. Read the diff.
- No internet lookup: no docs, no Stack Overflow, no package installs. It works from your repo and the model's weights.
- Context windows are small on consumer hardware; big files are read in windows and long histories are trimmed.
- `--parallel 2+` is only safe for genuinely independent steps. Two agents editing the same file will clobber each other; keep it at 1 when steps are coupled.
- CPU-only inference is slow. A 7B run of a few steps takes minutes.
- Prebuilt llama.cpp builds are per-platform; a USB stick built on x86-64 Linux will not run on an Apple Silicon laptop. Run the builder once per platform, or install Ollama on the target.
- The security review is a finite checklist, not a proof. It cannot see a secret computed at runtime, and it does not watch the network.
- Full-disk encryption remains an OS operation. OffGrid encrypts chosen files and folders only, and cannot erase their plaintext.

## License

MIT. Yours to fork, ship and sell. The models you download have their own licences (Qwen2.5-Coder is Apache-2.0).

# Troubleshooting

## `offgrid` says it cannot reach a model

Start one local OpenAI-compatible server, then run `offgrid doctor`:

```console
ollama serve
ollama pull qwen2.5-coder:7b

# or a downloaded llama.cpp build:
llama-server -m Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf --port 8080 --jinja
```

OffGrid probes ports 8080 (llama.cpp), 11434 (Ollama), 1234 (LM Studio), 8000
(vLLM), and 5000 (text-generation-webui) on loopback. A remote endpoint needs
`--allow-remote`.

## `offgrid demo` is the quickest check

It needs no model, downloads, account, or network:

```console
offgrid demo --no-color
```

If that succeeds, the package and orchestration work; a failure is about the
model server or your project.

## The installed wheel builds an incomplete USB bundle

This was a real defect before `0.1.0`: an installed wheel lacked `launch/`,
`scripts/`, `README.md`, and `LICENSE`, so `offgrid distribute --build`
succeeded with a zip that could not launch.

Reinstall the current wheel or run from a source checkout. The builder now
locates `share/offgrid` data files and fails with a clear list if any are
missing. To verify a build:

```console
mkdir -p /tmp/offgrid-work
cd /tmp/offgrid-work
offgrid distribute --build /tmp/offgrid-portable.zip
python3 - <<'PY'
import zipfile
names = set(zipfile.ZipFile("/tmp/offgrid-portable.zip").namelist())
for required in ("README.md", "LICENSE", "OffGrid.sh", "launch/offgrid.sh", "scripts/setup_usb.py"):
    assert any(name.endswith(required) for name in names), required
print("bundle looks complete")
PY
```

## The 35B model does not fit or is very slow

The 35B option is Qwen3.6-35B-A3B Q4_K_M, about 20.5 GB on disk. It is a
mixture-of-experts model with roughly 3B active parameters, but the full file
still has to be read and the machine should have about 32 GB of RAM. A 16 GB
machine should use `7b` or `14b`. CPU-only inference will be slow even when the
file fits; a fast disk and enough memory matter.

```console
python3 scripts/setup_usb.py --dry-run --model 35b
python3 scripts/setup_usb.py --target /media/USB/offgrid --model 35b
```

Model downloads are checked against a pinned SHA-256. A mismatch deletes the
partial download and reports the expected and actual hash.

## The 80B model does not fit or is very slow

The opt-in `80b` option is Qwen3-Next-80B-A3B-Instruct Q4_K_M, about 48.4 GB on
disk. It has 80B total parameters and roughly 3B active, but the full file
still has to be resident or readable. It needs at least 64 GB of RAM (96 GB is
recommended) and about 50 GB of free space. A 32 GB machine can only run it by
paging weights from disk, which is very slow and depends on a fast SSD; use
`35b` or smaller for that class of machine.

The wizard warns when the detected RAM is below 64 GB and checks free space
before starting the download. The direct builder does the same preflight; if
space is short, choose another target or free space and retry.

```console
python3 scripts/setup_usb.py --dry-run --model 80b
python3 scripts/setup_usb.py --target /media/USB/offgrid --model 80b
```

## The drive check says the USB stick is not exFAT

Large GGUF files cannot live on FAT32, which has a 4 GB per-file limit. Format
the drive as exFAT, then re-run the check:

```console
offgrid distribute --check-drive /media/USB --bundle dist/offgrid-portable.zip
```

The check also rejects a drive that is not writable or lacks space for the
bundle plus a 1 GB margin. For a full bundle directory, pass that directory as
`--bundle`; it checks every `.gguf` against the pinned model hash. A model with
no pinned hash cannot be verified and is rejected. The normal `--to` install
path performs the same check for mounted device roots and accepts the same
full-bundle directory, copying it after the check.

## A signed backup will not verify

A backup with `SHA256SUMS.sig` must be checked with the same key that signed it:

```console
offgrid protect --check --backup /media/USB --sign-key ~/.offgrid-backup.key
offgrid protect --restore /media/USB --sign-key ~/.offgrid-backup.key
```

If the key is lost, the signature cannot be verified. The key is deliberately
not stored in the backup. An unsigned backup still detects accidental change
through its hashes, but not deliberate rehashing.

## Restore refuses because files already exist

`protect --restore` is intentionally non-destructive. Move the old files, or
use `--force` only after you have checked what will be replaced:

```console
offgrid protect --restore /media/USB --workspace . --force
```

Use `--out` for a different kit path and `--model-out` for a different model
path.

## The local state directory has unexpected permissions

On POSIX, OffGrid creates `.offgrid` as `0700` and private files as `0600`. If
you created the directory yourself, fix it:

```console
chmod 700 .offgrid
find .offgrid -type f -exec chmod 600 {} +
```

Windows uses ACLs; POSIX mode bits are not the security boundary there.

## Fleet is on but peers do not answer

Fleet is off unless you pass `--fleet` or run `fleet serve`. Peers must be on a
private LAN, use the same shared token, and be reachable on port 8776 (with
8777 for discovery). Fleet traffic is cleartext HTTP, not TLS: do not expose it
to the internet.

## `--speak` is silent

OffGrid uses a voice that is already installed; it does not bundle a TTS
engine. Check the engine directly:

```console
# macOS
say "hello"
say -v ?                    # list voice names

# Debian / Ubuntu Linux
sudo apt install espeak-ng
espeak-ng "hello"

# Windows PowerShell
Add-Type -AssemblyName System.Speech
(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices().VoiceInfo.Name
```

If `--voice` names a voice the engine does not have, the run continues and
prints one warning. Omit `--voice` to use the system default. `--speak` only
reads responses aloud; microphone input is not implemented.

## A self-improvement request is waiting or broke the workspace

Nothing is applied until you approve it. List and inspect pending requests:

```console
offgrid improve
offgrid improve show ID
offgrid improve reject ID
```

If `approve` says the workspace changed after the proposal, the model tried to
overwrite a file you edited. Re-propose instead, or use `--force` only when you
intend to discard that later edit. If live verification fails, the request is
rolled back automatically. A successful application can be undone with:

```console
offgrid improve restore ID
offgrid improve undo
```

`restore` also refuses when those files changed after application; `--force`
discards those later edits. If a previous rollback was incomplete, inspect the
request under `.offgrid/self-improve/requests/ID/` and retry with
`offgrid improve restore ID --force`. These snapshots cover only the files in
that request, not the whole repository; keep using git or your own backup.

## A continuous maintenance pass reports FAIL

Run one pass with JSON output to see exactly which self-debug check failed:

```console
offgrid maintain --once --json
```

`workspace` or `state` failures are about local permissions or a missing
workspace. `requests` means a folder under `.offgrid/self-improve/requests`
cannot be read; inspect that request before deleting anything. `model` means the
local server is not answering or the configured model is unavailable. `disk`
means less than 1 GB is free. Maintenance still repairs state permissions and
prunes only old `rejected`, `restored`, or `rolled_back` requests; it never
removes pending or applied snapshots and never applies code. A failed `--improve`
pass leaves the workspace unchanged.

## A command was refused

`ask` is the default and prompts for each shell command. `--auto` still refuses
destructive commands, and `--no-shell` disables the shell entirely. If a command
is blocked by a false positive, run the safe part yourself and report the rule
so it can be improved.

## The security review reports a secret

The finding masks the value, but the file still contains it. Remove it from the
file and, if it was a real credential, rotate it. Deleting the line is not
enough if it was committed or copied into a backup.

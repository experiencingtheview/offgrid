# Security

OffGrid is local-first software for running coding agents against a model on
your own machine. This document describes what it protects, what it does not,
and how to report a problem.

## Reporting a vulnerability

Please use GitHub's private **Security → Report a vulnerability** flow on the
project repository. Do not open a public issue that includes an exploit, a
credential, or another person's data. Include the version, platform, a minimal
reproduction, and what an attacker gains.

There is no bug bounty and no guaranteed response time. Good-faith reports are
welcome.

## Threat model

OffGrid is designed around these boundaries:

- **No telemetry and no accounts.** The code has no analytics, update check, or
  central service. Network connections are opt-in: the model endpoint you
  configure, optional library/model downloads when you explicitly run a builder,
  fleet or public-council calls, and explicit `offgrid sync` peers.
- **Loopback by default.** A non-local model endpoint is refused unless
  `--allow-remote` or the corresponding configuration is set. Public council
  members require `--council-public`.
- **Workspace confinement.** File tools resolve paths against the workspace and
  refuse paths outside it, including symlink escapes.
- **Command policy.** `ask` is the default; `deny` removes the shell; `auto`
  still refuses destructive commands and asks for approval on elevated or
  irreversible ones.
- **Local state is private.** On POSIX, `.offgrid` is set to `0700` and session
  logs, memory, security history, the continuity kit, baselines, and the sync
  token are set to `0600`. Windows uses the account's ACLs; the POSIX chmod
  calls are best-effort there.
- **Reviewable code.** The runtime uses only the Python standard library and
  does not hide network calls behind a dependency.

## Known limits

- **Fleet is cleartext HTTP.** Fleet peers authenticate with a shared bearer
  token, but model prompts and answers are not encrypted in transit. Use it only
  on a trusted private LAN. Do not port-forward it or expose it to an untrusted
  network. The token proves possession; it does not provide confidentiality.
- **Sync is explicit and quarantined.** `offgrid sync` shares accepted learning
  notes only; incoming notes are pending and cannot reach a prompt until a local
  human accepts them. Peers need a shared bearer token. Public peers require
  HTTPS from the client, but the built-in server speaks HTTP: terminate TLS in a
  reverse proxy before exposing it to the internet. Plain HTTP to a non-loopback
  peer is refused unless `--allow-http` / `sync_allow_http` is set explicitly.
  The token authenticates a peer; it does not encrypt traffic.
- **A local model is not a security boundary.** Prompt injection in a file, a
  tool result, or a repository can influence the model. The deterministic
  command policy and path checks are the enforcement; model output is untrusted.
- **Backups are authenticated, not identity-signed.** `protect --backup` writes
  SHA-256 hashes. With `--sign-key`, it adds an HMAC-SHA256 signature. Anyone
  who has the key can re-sign an altered manifest, so keep the key off the
  backup drive. A path cannot prove the drive is physically off the machine.
- **Model downloads are checked when a hash is known.** The bundled 3B, 7B, 14B,
  and 35B GGUF files have pinned SHA-256 hashes and are deleted on mismatch.
  The portable Python and llama.cpp archives are fetched over HTTPS from their
  release pages but are not pinned to a hash by this project; verify those
  upstream artifacts if that matters to your threat model.
- **No secure deletion.** Encrypting a chosen file or folder does not erase its
  plaintext. Full-disk encryption is an OS operation.
- **The security review is a checklist.** It cannot see a secret computed at
  runtime, and `security watch` observes this machine's connection table, not
  other devices or a phone tap.

## Reproducible checks

```console
python3 -m unittest discover -s tests -t .
python3 -m offgrid demo --no-color
python3 -m offgrid security review
```

For a release, follow [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md).

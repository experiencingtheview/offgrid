/**
 * The download the site hands out: three launchers and a first-run note, packed
 * into one zip.
 *
 * The launchers are the whole "installer". Each one opens a terminal, fetches
 * this repository once if it is not on the machine yet, runs `offgrid setup`
 * (which reports the interpreter, finds a local model server and explains the
 * free ways to start one), and then starts the chat. Nothing is installed,
 * nothing runs as root, and the only request made is the source archive the
 * file names out loud.
 *
 * The zip is built here rather than committed, so the launchers stay reviewable
 * as plain text. Entries are stored (not deflated) and carry unix modes, which
 * is what makes a double-clicked .command executable after extraction.
 */

const DOS_DATE = 33; // 1980-01-01, fixed so the archive is byte-identical per run

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let value = i;
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    }
    table[i] = value >>> 0;
  }
  return table;
})();

const SOURCE_ARCHIVE =
  "https://github.com/experiencingtheview/offgrid/archive/refs/heads/main.tar.gz";

const README = `OffGrid setup
=============

What this is
------------
Three tiny launchers and this note. Nothing is installed and nothing runs as
an administrator: a launcher opens a terminal, downloads OffGrid into your home
folder once, runs its setup check, and then starts the chat.

Pick the file for your computer
-------------------------------
macOS     OffGrid.command   double-click it in Finder
Linux     OffGrid.sh        chmod +x OffGrid.sh && ./OffGrid.sh
Windows   OffGrid.bat       double-click it

On macOS you can also run it from a terminal with ./OffGrid.command, and on
Windows run OffGrid.bat from a Command Prompt if double-clicking closes too fast.

What happens on the first run
-----------------------------
1. It looks for Python 3.9 or newer and stops with the download link if the
   interpreter is missing or too old.
2. It fetches ${SOURCE_ARCHIVE}
   once into ~/OffGrid (Windows: %USERPROFILE%\\OffGrid). Already there? It is
   reused, so later runs skip the network entirely.
3. It runs "python3 -m offgrid setup": interpreter, platform, workspace, and a
   scan of localhost for a model server. If none answers it shows the two free
   options (Ollama and llama.cpp) with the exact commands.
4. Only when a model server answers does it start the chat. Otherwise it stops
   and tells you what to start first, and running the launcher again picks up
   where you left off.

What you need
-------------
- Python 3.9 or newer, from python.org. On Windows, tick "Add python.exe to
  PATH"; the Microsoft Store alias is not a working interpreter.
- A local OpenAI-compatible model server: Ollama, llama.cpp, LM Studio or vLLM.
- Internet for the first download only. After that OffGrid runs offline.

Why it is safe to read first
----------------------------
Both launchers are plain text, a few dozen lines each: open them in any editor.
They download exactly one URL, given above, and send nothing anywhere. There is
no telemetry in OffGrid and no account, key or cloud service involved.

Questions or problems: https://github.com/experiencingtheview/offgrid
`;

function shellLauncher(platform) {
  const header =
    platform === "macos"
      ? "macOS: double-click this file in Finder and Terminal opens by itself."
      : "Linux: run ./OffGrid.sh from a terminal (chmod +x first if needed).";
  return `#!/bin/sh
# OffGrid setup launcher.
# ${header}
#
# It needs Python 3.9+ and internet for the first download only. Nothing is sent
# back anywhere: the single request is the source archive named below.

set -u
cd "$(dirname "$0")" || exit 1

ARCHIVE=${SOURCE_ARCHIVE}
SRC=\${OFFGRID_HOME:-$HOME/OffGrid}

say() { printf '%s\\n' "$*"; }
stop() { say ""; say "$*"; printf 'Press return to close. '; read -r _; exit 1; }

if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else stop "OffGrid needs Python 3.9 or newer: https://www.python.org/downloads/"
fi

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \\
  || stop "That Python is older than 3.9. Install a newer one, then run this again."

if [ ! -f "$SRC/offgrid/cli.py" ]; then
  say "Downloading OffGrid into $SRC (this happens once)..."
  mkdir -p "$SRC" || stop "Could not create $SRC."
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 2 -o "$SRC/.offgrid-src.tar.gz" "$ARCHIVE" \\
      || { rm -f "$SRC/.offgrid-src.tar.gz"; stop "The download failed. Check the connection, then run this again."; }
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$SRC/.offgrid-src.tar.gz" "$ARCHIVE" \\
      || { rm -f "$SRC/.offgrid-src.tar.gz"; stop "The download failed. Check the connection, then run this again."; }
  else
    stop "OffGrid needs curl or wget for the one download. Install one, then run this again."
  fi
  tar -xzf "$SRC/.offgrid-src.tar.gz" -C "$SRC" --strip-components=1 \\
    || stop "Could not unpack the download into $SRC."
  rm -f "$SRC/.offgrid-src.tar.gz"
fi

cd "$SRC" || stop "Could not enter $SRC."
say ""
say "Checking this machine..."
"$PY" -m offgrid setup \\
  || stop "Setup is not finished. Fix what it reported, then run this launcher again."

say ""
say "Starting OffGrid. Type your task at the prompt; /quit leaves."
exec "$PY" -m offgrid chat
`;
}

const WINDOWS_LAUNCHER = `@echo off
rem OffGrid setup launcher (Windows).
rem Double-click it. It needs Python 3.9+ and internet for the first download
rem only; the single request is the source archive named below.

setlocal
title OffGrid setup
cd /d "%~dp0"

set "SRC=%USERPROFILE%\\OffGrid"
set "ARCHIVE=${SOURCE_ARCHIVE}"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

if exist "%SRC%\\offgrid\\cli.py" goto run

echo Downloading OffGrid into %SRC% (this happens once)...
if not exist "%SRC%" mkdir "%SRC%"
curl -fL --retry 2 -o "%SRC%\\.offgrid-src.tar.gz" "%ARCHIVE%"
if errorlevel 1 goto baddownload
tar -xzf "%SRC%\\.offgrid-src.tar.gz" -C "%SRC%" --strip-components=1
if errorlevel 1 goto badunpack
del "%SRC%\\.offgrid-src.tar.gz" >nul 2>nul

:run
cd /d "%SRC%"
echo.
echo Checking this machine...
%PY% -m offgrid setup
if errorlevel 1 goto notready
echo.
echo Starting OffGrid. Type your task at the prompt; /quit leaves.
%PY% -m offgrid chat
goto done

:nopython
echo OffGrid needs Python 3.9 or newer: https://www.python.org/downloads/windows/
echo Tick "Add python.exe to PATH" while installing, then run this again.
goto fail

:oldpython
echo That Python is older than 3.9. Install a newer one from python.org, then run this again.
goto fail

:baddownload
del "%SRC%\\.offgrid-src.tar.gz" >nul 2>nul
echo The download failed. Check the connection, then run this again.
goto fail

:badunpack
echo Could not unpack the download into %SRC%.
goto fail

:notready
echo.
echo Setup is not finished. Fix what it reported, then run this launcher again.
goto fail

:fail
echo.
pause
exit /b 1

:done
`;

/** Everything the zip contains, in order. */
export const SETUP_FILES = [
  { name: "OffGrid.command", text: shellLauncher("macos"), mode: 0o755 },
  { name: "OffGrid.sh", text: shellLauncher("linux"), mode: 0o755 },
  { name: "OffGrid.bat", text: WINDOWS_LAUNCHER, mode: 0o644 },
  { name: "README-FIRST.txt", text: README, mode: 0o644 },
];

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) {
    crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function concat(chunks) {
  let total = 0;
  for (const chunk of chunks) total += chunk.length;
  const out = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.length;
  }
  return out;
}

/** A stored (uncompressed) zip with the given `{ name, text|bytes, mode }` files. */
export function buildZip(files) {
  const encoder = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;

  for (const file of files) {
    const name = encoder.encode(file.name);
    const data = file.bytes ?? encoder.encode(file.text ?? "");
    const checksum = crc32(data);

    const local = new Uint8Array(30 + name.length);
    const localView = new DataView(local.buffer);
    localView.setUint32(0, 0x04034b50, true); // local file header
    localView.setUint16(4, 20, true); // version needed
    localView.setUint16(6, 0x0800, true); // names are UTF-8
    localView.setUint16(8, 0, true); // stored
    localView.setUint16(10, 0, true); // time
    localView.setUint16(12, DOS_DATE, true);
    localView.setUint32(14, checksum, true);
    localView.setUint32(18, data.length, true);
    localView.setUint32(22, data.length, true);
    localView.setUint16(26, name.length, true);
    localView.setUint16(28, 0, true);
    local.set(name, 30);

    const entry = new Uint8Array(46 + name.length);
    const entryView = new DataView(entry.buffer);
    entryView.setUint32(0, 0x02014b50, true); // central directory header
    entryView.setUint16(4, 0x0314, true); // made on unix, zip 2.0
    entryView.setUint16(6, 20, true);
    entryView.setUint16(8, 0x0800, true);
    entryView.setUint16(10, 0, true);
    entryView.setUint16(12, 0, true);
    entryView.setUint16(14, DOS_DATE, true);
    entryView.setUint32(16, checksum, true);
    entryView.setUint32(20, data.length, true);
    entryView.setUint32(24, data.length, true);
    entryView.setUint16(28, name.length, true);
    entryView.setUint32(38, (file.mode ?? 0o644) << 16, true); // unix mode
    entryView.setUint32(42, offset, true);
    entry.set(name, 46);

    parts.push(local, data);
    central.push(entry);
    offset += local.length + data.length;
  }

  const centralSize = central.reduce((total, entry) => total + entry.length, 0);
  const end = new Uint8Array(22);
  const endView = new DataView(end.buffer);
  endView.setUint32(0, 0x06054b50, true); // end of central directory
  endView.setUint16(8, files.length, true);
  endView.setUint16(10, files.length, true);
  endView.setUint32(12, centralSize, true);
  endView.setUint32(16, offset, true);

  return concat([...parts, ...central, end]);
}

/** The zip as a download response, with a filename browsers respect. */
export function zipResponse() {
  return new Response(buildZip(SETUP_FILES), {
    headers: {
      "content-type": "application/zip",
      "content-disposition": 'attachment; filename="offgrid-setup.zip"',
      "cache-control": "public, max-age=300",
      "x-content-type-options": "nosniff",
    },
  });
}

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SETUP_FILES,
  buildZip,
  crc32,
  zipResponse,
} from "./site-archive.js";

/** Read a stored zip back the way an unzip tool would. */
function parse(zip) {
  const view = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
  let end = -1;
  for (let at = zip.length - 22; at >= 0; at -= 1) {
    if (view.getUint32(at, true) === 0x06054b50) {
      end = at;
      break;
    }
  }
  assert.ok(end >= 0, "the archive has an end-of-central-directory record");

  const count = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  const entries = [];
  for (let index = 0; index < count; index += 1) {
    assert.equal(view.getUint32(at, true), 0x02014b50, "central directory header");
    const nameLength = view.getUint16(at + 28, true);
    const extraLength = view.getUint16(at + 30, true);
    const commentLength = view.getUint16(at + 32, true);
    const name = new TextDecoder().decode(
      zip.subarray(at + 46, at + 46 + nameLength),
    );
    const checksum = view.getUint32(at + 16, true);
    const size = view.getUint32(at + 24, true);
    const mode = (view.getUint32(at + 38, true) >>> 16) & 0o777;
    const localOffset = view.getUint32(at + 42, true);

    assert.equal(view.getUint32(localOffset, true), 0x04034b50, "local header");
    const localNameLength = view.getUint16(localOffset + 26, true);
    const localExtraLength = view.getUint16(localOffset + 28, true);
    const start = localOffset + 30 + localNameLength + localExtraLength;
    entries.push({
      name,
      mode,
      checksum,
      data: zip.subarray(start, start + size),
    });
    at += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

const entries = parse(buildZip(SETUP_FILES));
const byName = Object.fromEntries(entries.map((entry) => [entry.name, entry]));

test("the download carries one launcher per desktop platform", () => {
  assert.deepEqual(
    entries.map((entry) => entry.name),
    ["OffGrid.command", "OffGrid.sh", "OffGrid.bat", "README-FIRST.txt"],
  );
});

test("each entry's checksum matches the bytes stored beside it", () => {
  for (const entry of entries) {
    assert.equal(entry.checksum, crc32(entry.data), entry.name);
  }
});

test("double-clicked launchers keep their executable bit", () => {
  assert.equal(byName["OffGrid.command"].mode, 0o755);
  assert.equal(byName["OffGrid.sh"].mode, 0o755);
  assert.equal(byName["OffGrid.bat"].mode, 0o644);
  assert.equal(byName["README-FIRST.txt"].mode, 0o644);
});

test("every launcher runs the setup check and then the chat", () => {
  for (const name of ["OffGrid.command", "OffGrid.sh", "OffGrid.bat"]) {
    const text = new TextDecoder().decode(byName[name].data);
    assert.ok(text.includes("-m offgrid setup"), name);
    assert.ok(text.includes("-m offgrid chat"), name);
    assert.ok(text.includes("Python 3.9"), name);
  }
});

test("the launchers name the single URL they download", () => {
  for (const name of ["OffGrid.command", "OffGrid.sh", "OffGrid.bat"]) {
    const text = new TextDecoder().decode(byName[name].data);
    assert.ok(
      text.includes(
        "https://github.com/experiencingtheview/offgrid/archive/refs/heads/main.tar.gz",
      ),
      name,
    );
    // The source archive is named once, so there is no second endpoint to audit
    // beyond the python.org link the same file prints when Python is missing.
    assert.equal(
      text.split(
        "https://github.com/experiencingtheview/offgrid/archive/refs/heads/main.tar.gz",
      ).length - 1,
      1,
      name,
    );
  }
});

test("the first-run note explains the download before it happens", () => {
  const note = new TextDecoder().decode(byName["README-FIRST.txt"].data);
  assert.ok(note.includes("Python 3.9"));
  assert.ok(note.includes("Ollama"));
  assert.ok(note.includes("offgrid setup"));
  assert.ok(note.toLowerCase().includes("no telemetry"));
});

test("the route answers with a named zip a browser will save", () => {
  const response = zipResponse();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/zip");
  assert.equal(
    response.headers.get("content-disposition"),
    'attachment; filename="offgrid-setup.zip"',
  );
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
});

test("the same archive is produced twice, byte for byte", () => {
  assert.deepEqual(buildZip(SETUP_FILES), buildZip(SETUP_FILES));
});

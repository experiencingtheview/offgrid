# Image compression

`offgrid images compress` makes image files smaller without adding Pillow, an
image service, or an external optimizer.

```console
# write smaller copies beside the originals
offgrid images compress screenshots/

# write into a separate tree, preserving relative paths
offgrid images compress screenshots/ --output compressed/

# replace the originals atomically
offgrid images compress screenshots/ --in-place
```

## What it does

### PNG: lossless recompression

The PNG is parsed chunk by chunk, its image data is decompressed, recompressed
with zlib's strongest setting, and written back with new checksums. Pixels,
colour data, transparency, animation chunks and ancillary metadata are not
changed. If the rebuilt stream is not smaller, the original is left alone.

This is a lossless recompression pass, not a resizer and not a lossy encoder.
Some PNGs are already optimized and will report `already-optimal`.

### JPEG: optional metadata removal

JPEG pixels are never decoded or re-encoded. With `--strip-metadata`, marker
segments such as APPn and COM before the image scan are removed. That can save
space and remove camera/EXIF fields, but it also removes EXIF orientation, so a
photo that relied on an orientation tag may display differently. An embedded
ICC colour profile in APP2 can be removed too, so colours may shift on a
colour-managed display. The flag is therefore opt-in.

A JPEG without `--strip-metadata` is left unchanged and reports
`already-optimal`; this build does not perform lossy JPEG quality reduction.

## Safety and limits

- Input files are limited to 64 MB, and decompressed PNG data is limited to
  128 MB to avoid compression bombs.
- PNG chunk checksums are validated; corrupt, truncated or unsupported files
  are refused.
- The default writes `name.compressed.ext` and never overwrites an existing
  file. `--in-place` is the only way to replace a source and uses a temporary
  file followed by an atomic replace.
- `--output` writes below a directory while preserving paths relative to the
  workspace. It is mutually exclusive with `--in-place`.
- Paths are confined to the workspace by the normal OffGrid workspace rules.
- Directory arguments are searched recursively for `.png`, `.jpg` and `.jpeg`.
- No network access is used and no image content is sent anywhere.
- This command optimizes files; it does not deliver images, resize them,
  convert formats, add alt text, or perform OCR.

## Exit status

`0` when every requested image was processed (including no-gain results), `1`
when any file failed, and `2` for a usage or path error. `--json` emits one
object with per-file results and the total saved bytes.

## Tests

The tests construct PNGs and JPEG metadata locally, then verify lossless IDAT
round-trips, smaller output, atomic in-place behavior, no-overwrite protection,
metadata stripping and rejection of corrupt files. No network or external image
tool is involved.

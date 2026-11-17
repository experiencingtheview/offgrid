"""Reliable downloads for the files a first run needs.

Setup files are large enough that a dropped connection should not throw away
the bytes already received.  This module keeps a private ``.part`` file, asks
the server to resume it with a Range request, retries transient failures, and
moves it into place only after the SHA-256 matches.  A failed attempt leaves a
resumable partial file behind; a checksum failure removes it because resuming
corrupt bytes cannot help.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

CHUNK = 256 * 1024
DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT = 120.0
_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
_FATAL_WRITE_ERRNOS = {
    errno.EACCES,
    errno.EPERM,
    errno.EROFS,
    errno.EISDIR,
}
_DISK_FULL_ERRNOS = {
    errno.ENOSPC,
    getattr(errno, "EDQUOT", errno.ENOSPC),
    getattr(errno, "EFBIG", errno.ENOSPC),
}


class DownloadError(Exception):
    """A download could not be completed safely."""


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 of *path* without loading it in memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_expected(value: str) -> str:
    expected = str(value or "").strip().lower()
    if not expected:
        return ""
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise DownloadError(
            "The download is missing a valid SHA-256 checksum; refusing to install it."
        )
    return expected


def _disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in _DISK_FULL_ERRNOS


def _disk_full_message(destination: Path) -> str:
    return (
        f"There is not enough free disk space to download {destination.name}. "
        "Free some space and try again; the partial download is kept so the "
        "next attempt can resume."
    )


def _content_range_start(value: str) -> Optional[int]:
    """Return the first byte in ``bytes 10-19/20`` or None if malformed."""
    text = str(value or "").strip()
    if not text.lower().startswith("bytes "):
        return None
    first, _separator, _rest = text[6:].partition("/")
    start, _dash, _end = first.partition("-")
    try:
        return int(start)
    except ValueError:
        return None


def _content_range_total(value: str) -> Optional[int]:
    text = str(value or "").strip()
    if "/" not in text:
        return None
    raw = text.rsplit("/", 1)[1].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _response_total(response, resume: int) -> Optional[int]:
    raw_length = response.headers.get("Content-Length")
    try:
        length = int(raw_length) if raw_length is not None else None
    except (TypeError, ValueError):
        length = None
    if length is not None and length >= 0:
        total = resume + length if getattr(response, "status", None) == 206 else length
    else:
        total = None
    content_range = response.headers.get("Content-Range", "")
    ranged_total = _content_range_total(content_range)
    if ranged_total is not None and ranged_total > 0:
        if total is None or ranged_total > total:
            total = ranged_total
    return total


def _progress(progress: Optional[Callable[[int, Optional[int]], None]], done: int, total: Optional[int]) -> None:
    if progress is None:
        return
    try:
        progress(max(0, done), total)
    except Exception:
        # A UI callback must never corrupt or abort the transfer.
        pass


def _replace_part(part: Path, destination: Path) -> None:
    try:
        os.replace(part, destination)
    except OSError as exc:
        if _disk_full(exc):
            raise DownloadError(_disk_full_message(destination)) from exc
        raise DownloadError(
            f"The download finished, but {destination.name} could not be saved: {exc}"
        ) from exc


def download(
    url: str,
    destination: Path,
    expected_sha256: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    progress: Optional[Callable[[int, Optional[int]], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    user_agent: str = "offgrid-download/1",
) -> Path:
    """Download *url* into *destination* reliably.

    The finished file is only visible at *destination* after the whole body and
    its checksum are verified.  Failed transfers leave ``<destination>.part``
    behind for the next call to resume.  A checksum mismatch deletes the partial
    file because resuming it would repeat the bad bytes.
    """
    destination = Path(destination)
    part = destination.with_name(destination.name + ".part")
    expected = _clean_expected(expected_sha256)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if _disk_full(exc):
            raise DownloadError(_disk_full_message(destination)) from exc
        raise DownloadError(
            f"Could not create the folder for {destination.name}: {exc}"
        ) from exc

    if destination.is_file() and destination.stat().st_size > 0:
        if not expected:
            return destination
        try:
            if sha256_file(destination) == expected:
                return destination
        except OSError as exc:
            raise DownloadError(
                f"The existing {destination.name} could not be checked: {exc}"
            ) from exc
        try:
            destination.unlink()
        except OSError as exc:
            raise DownloadError(
                f"The damaged {destination.name} could not be removed: {exc}"
            ) from exc

    tries = max(1, int(attempts))
    last_reason = "the connection was interrupted"
    checksum_failed = False

    for attempt in range(1, tries + 1):
        try:
            resume = part.stat().st_size if part.is_file() else 0
        except OSError:
            resume = 0
        headers = {"User-Agent": user_agent}
        if resume:
            headers["Range"] = f"bytes={resume}-"
        request = urllib.request.Request(url, headers=headers)

        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                exc.close()
            except Exception:
                pass
            if exc.code == 416 and resume:
                # The server says our partial file is already at the end.  It
                # is only complete if the checksum says so.
                try:
                    complete = sha256_file(part) if expected else ""
                except OSError:
                    complete = ""
                if not expected or complete == expected:
                    _replace_part(part, destination)
                    return destination
                part.unlink(missing_ok=True)
                last_reason = "the server rejected the partial file"
                if attempt < tries:
                    sleep(min(5.0, float(attempt)))
                continue
            if exc.code in _TRANSIENT_HTTP:
                last_reason = f"the server returned HTTP {exc.code}"
                if attempt < tries:
                    sleep(min(5.0, float(attempt)))
                continue
            raise DownloadError(
                f"Could not download {destination.name}: the server returned HTTP "
                f"{exc.code}."
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            if _disk_full(exc):
                raise DownloadError(_disk_full_message(destination)) from exc
            last_reason = f"the connection failed ({exc})"
            if attempt < tries:
                sleep(min(5.0, float(attempt)))
            continue

        try:
            status = getattr(response, "status", None)
            if resume and status == 206:
                start = _content_range_start(response.headers.get("Content-Range", ""))
                if start != resume:
                    # Never append a response that starts somewhere else.
                    part.unlink(missing_ok=True)
                    last_reason = "the server sent a mismatched resume range"
                    if attempt < tries:
                        sleep(min(5.0, float(attempt)))
                    continue
                mode = "ab"
                base = resume
            elif resume:
                # A server that ignored Range would make us append a second
                # whole file.  Start over instead.
                part.unlink(missing_ok=True)
                resume = 0
                mode = "wb"
                base = 0
            else:
                mode = "wb"
                base = 0

            total = _response_total(response, base)
            _progress(progress, base, total)
            done = base
            try:
                with part.open(mode) as handle:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        done += len(chunk)
                        _progress(progress, done, total)
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError as exc:
                        if _disk_full(exc):
                            raise DownloadError(_disk_full_message(destination)) from exc
                        # Some filesystems do not support fsync; the checksum
                        # still protects the completed file.
                        pass
            except (OSError, http.client.HTTPException) as exc:
                if _disk_full(exc):
                    raise DownloadError(_disk_full_message(destination)) from exc
                if isinstance(exc, OSError) and exc.errno in _FATAL_WRITE_ERRNOS:
                    raise DownloadError(
                        f"Could not write {destination.name}: {exc}. Check the "
                        "folder's permissions and try again."
                    ) from exc
                last_reason = f"writing {destination.name} was interrupted ({exc})"
                if attempt < tries:
                    sleep(min(5.0, float(attempt)))
                continue

            if total is not None and done < total:
                last_reason = (
                    f"the connection closed after {done} of {total} bytes"
                )
                if attempt < tries:
                    sleep(min(5.0, float(attempt)))
                continue
            if total is not None and done > total:
                part.unlink(missing_ok=True)
                last_reason = "the server sent more data than it announced"
                if attempt < tries:
                    sleep(min(5.0, float(attempt)))
                continue

            if expected:
                try:
                    actual = sha256_file(part)
                except OSError as exc:
                    last_reason = f"the downloaded file could not be checked ({exc})"
                    if attempt < tries:
                        sleep(min(5.0, float(attempt)))
                    continue
                if actual != expected:
                    part.unlink(missing_ok=True)
                    checksum_failed = True
                    last_reason = (
                        f"the SHA-256 checksum did not match (expected {expected}, "
                        f"got {actual})"
                    )
                    if attempt < tries:
                        sleep(min(5.0, float(attempt)))
                    continue

            _replace_part(part, destination)
            return destination
        finally:
            try:
                response.close()
            except Exception:
                pass

    if checksum_failed:
        raise DownloadError(
            f"{destination.name} did not match its SHA-256 checksum after "
            f"{tries} attempt(s). The damaged partial file was removed. Check "
            "the internet connection and try again."
        )
    raise DownloadError(
        f"{destination.name} was interrupted after {tries} attempt(s): "
        f"{last_reason}. The partial file was kept so the next attempt can "
        "resume."
    )

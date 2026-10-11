"""Reading the text layer of a PDF with pypdf.

A PDF with no usable text layer (a scan or a photo) is not guessed at: the caller sends
it to a person for manual entry. No OCR is attempted.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import queue
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

MIN_TEXT_CHARS = 30  # fewer readable characters than this in the whole file means "no text layer"


@dataclass
class PdfText:
    pages: list[str] = field(default_factory=list)
    page_count: int = 0
    problem: str | None = None  # set when the text cannot be used; the document needs manual entry
    busy: bool = False  # every worker was busy for too long; reading again later may work

    @property
    def text(self) -> str:
        return "\n".join(f"--- page {i} ---\n{t}" for i, t in enumerate(self.pages, 1))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def looks_like_pdf(data: bytes) -> bool:
    """A PDF starts with %PDF- (some writers put a few bytes of junk first; allow 1 KB)."""
    return b"%PDF-" in data[:1024]


def read_text(data: bytes, max_pages: int = 50) -> PdfText:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed files are expected input here
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):  # some PDFs are "encrypted" with an empty password
                    return PdfText(problem="The PDF is password protected.")
            except Exception:
                return PdfText(problem="The PDF is password protected.")
        count = len(reader.pages)
        if count == 0:
            return PdfText(problem="The PDF has no pages.")
        if count > max_pages:
            return PdfText(page_count=count, problem=f"The PDF has {count} pages; the limit is {max_pages}.")
        pages = [(page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n") for page in reader.pages]
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, RecursionError) as exc:
        return PdfText(problem=f"The PDF could not be read ({type(exc).__name__}).")
    except Exception as exc:  # pypdf raises a wide range of errors on broken files
        return PdfText(problem=f"The PDF could not be read ({type(exc).__name__}).")
    readable = sum(1 for ch in "".join(pages) if not ch.isspace())
    if readable < MIN_TEXT_CHARS:
        return PdfText(pages=pages, page_count=count,
                       problem="The PDF has no text layer (it looks scanned). It needs OCR or manual entry.")
    return PdfText(pages=pages, page_count=count)


# --------------------------------------------------------------------------- isolation
# A small PDF can still take minutes to parse (many text operations, deep nesting). Reading
# happens in a separate worker process (`python -m intake.pdftext`): if it runs past the time
# limit, the worker is killed and the document goes to a person. On Linux and macOS the worker
# also gets a memory limit; Windows has no simple per-process limit, so there only the time
# limit holds. The worker is a plain subprocess, so it never re-imports the caller's script.
WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
_HEAD = struct.Struct(">II")


def _send(stream: BinaryIO, header: dict, payload: bytes = b"") -> None:
    head = json.dumps(header).encode()
    stream.write(_HEAD.pack(len(head), len(payload)) + head + payload)
    stream.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks, left = [], size
    while left:
        chunk = stream.read(left)
        if not chunk:
            raise EOFError("the stream ended")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _recv(stream: BinaryIO) -> tuple[dict, bytes]:
    head_size, payload_size = _HEAD.unpack(_read_exact(stream, _HEAD.size))
    header = json.loads(_read_exact(stream, head_size))
    return header, _read_exact(stream, payload_size)


def _worker_main() -> None:
    try:
        import resource  # not on Windows

        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
    except Exception:
        pass
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            header, data = _recv(stdin)
        except (EOFError, OSError, ValueError):
            return
        result = read_text(data, int(header.get("max_pages", 50)))
        _send(stdout, {"pages": result.pages, "page_count": result.page_count, "problem": result.problem})


class _Worker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def _start(self) -> None:
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._proc = subprocess.Popen([sys.executable, "-m", "intake.pdftext"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self._proc = None

    def read(self, data: bytes, max_pages: int, timeout: float) -> PdfText:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            proc = self._proc
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            answers: queue.Queue = queue.Queue()

            def pump() -> None:  # a blocking read in a thread, so the wait below can time out
                try:
                    answers.put(_recv(proc.stdout)[0])
                except Exception as exc:
                    answers.put(exc)

            try:
                _send(proc.stdin, {"max_pages": max_pages}, data)
            except OSError:
                self._stop()
                return PdfText(problem="The PDF could not be read (the reader process stopped).")
            threading.Thread(target=pump, daemon=True).start()
            try:
                answer = answers.get(timeout=timeout)
            except queue.Empty:
                self._stop()
                return PdfText(problem=f"Reading the PDF took longer than {timeout:g} seconds, so it was stopped.")
            if isinstance(answer, Exception):
                self._stop()
                return PdfText(problem="The PDF could not be read (the reader process stopped).")
            return PdfText(pages=answer["pages"], page_count=answer["page_count"], problem=answer["problem"])


class _Pool:
    """A few worker processes, so one slow or hostile PDF holds up only its own worker. A PDF
    that cannot get a worker within `wait` seconds is not read now: the caller sends it to a
    person, or leaves an inbox file for the next scan."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._idle: queue.LifoQueue[_Worker] = queue.LifoQueue()
        for _ in range(size):
            self._idle.put(_Worker())  # each starts its process on first use

    def read(self, data: bytes, max_pages: int, timeout: float, wait: float) -> PdfText:
        try:
            worker = self._idle.get(timeout=max(0.0, wait))
        except queue.Empty:
            return PdfText(problem=f"The PDF reader was busy with other files for more than {wait:g} seconds, so "
                                   "this one was not read. Use 'Read the PDF again' in a moment.", busy=True)
        try:
            return worker.read(data, max_pages, timeout)
        finally:
            self._idle.put(worker)


_pools: dict[int, _Pool] = {}
_pools_lock = threading.Lock()


def _pool(size: int) -> _Pool:
    with _pools_lock:
        if size not in _pools:
            _pools[size] = _Pool(size)
        return _pools[size]


def read_text_isolated(data: bytes, max_pages: int = 50, timeout: float = 20.0, workers: int = 3,
                       wait: float = 30.0) -> PdfText:
    """read_text in a worker process with a time limit, `workers` at a time; a PDF waits at most
    `wait` seconds for a free worker. timeout <= 0 reads in this process (tests only)."""
    if timeout <= 0:
        return read_text(data, max_pages)
    return _pool(max(1, workers)).read(data, max_pages, timeout, wait)


if __name__ == "__main__":
    _worker_main()

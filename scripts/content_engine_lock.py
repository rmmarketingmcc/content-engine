"""One lock for every write to the content engine's Google Doc.

The drafter, the photo job and the feedback job all edit the batch tabs by character positions. Two of them editing
the same tab at once can land a deletion on the wrong heading, and the next feedback run then reads every post one
off. Every function that changes the doc runs under this lock (re-entrant within a process, an flock across
processes), so two jobs never edit the doc at once.
"""
import contextlib
import fcntl
import functools
import threading

import ce_config as C

LOCK = C.CACHE / "doc.lock"
_held = threading.local()


@contextlib.contextmanager
def doc_lock(timeout_note: str = ""):
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # waits for the other job to finish its edit
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


def locked(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        with doc_lock():
            return fn(*a, **k)
    return run

"""Small file helpers shared by every module that writes local data."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO


def write_atomic(path: Path, data: bytes) -> None:
    """Write a file so readers see either the old or the new content, never a mix.

    The data goes to a temporary file in the same folder, which is then renamed over
    the target. A crash leaves the previous file intact.
    """
    staged = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=f'-{path.name}', delete=False) as handle:
            staged = Path(handle.name)
            handle.write(data)
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def try_lock(path: Path) -> BinaryIO | None:
    """Take an exclusive, non-blocking OS lock on ``path``.

    Returns the open file, which holds the lock until it is closed (or the process
    exits, even after a crash), or None if another process holds it.
    """
    stream = path.open('a+b')
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream

"""Nonblocking process locks for the index and plan rebuilds."""
import os

if os.name == "nt":
    import errno
    import msvcrt

    def try_lock(file):
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True
else:
    import fcntl

    def try_lock(file):
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

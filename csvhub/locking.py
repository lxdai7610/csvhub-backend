from contextlib import contextmanager
import os


class LockBusy(Exception):
    pass


@contextmanager
def file_lock(path):
    """Process-wide exclusive lock, released by the OS after a crash (Windows + POSIX)."""
    file = path.open("a+b")
    locked = False
    try:
        if path.stat().st_size == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise LockBusy("Another process is using this resource") from error
        yield
    finally:
        if locked:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        file.close()


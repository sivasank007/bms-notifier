"""Technical logging: rotating file + an in-memory tail for the debug endpoint."""
import logging
from collections import deque
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

TAIL: deque[str] = deque(maxlen=400)       # last lines, for /api/debug/logs

class _TailHandler(logging.Handler):
    def emit(self, record):
        TAIL.append(self.format(record))

def setup() -> logging.Logger:
    log = logging.getLogger("bmswatch")
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=5)
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG); log.addHandler(fh)

    th = _TailHandler(); th.setFormatter(fmt); th.setLevel(logging.DEBUG); log.addHandler(th)

    sh = logging.StreamHandler(); sh.setFormatter(fmt); sh.setLevel(logging.INFO)
    log.addHandler(sh)
    return log

log = setup()

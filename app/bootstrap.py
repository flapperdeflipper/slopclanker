"""First-boot bootstrap: legacy database steps aside, fresh v2 takes over.

A pre-1.0 database at the active path is never read or written — it is
renamed to slopclanker-legacy.db (first free -N suffix) and a fresh
schema-v2 database is created in its place.
"""

import logging
from pathlib import Path

from app import db

logger = logging.getLogger(__name__)


def ensure(db_file: str | Path) -> Path:
    """Return the active v2 path, renaming legacy aside / creating fresh."""
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if db.is_v2(path):
        return path
    if path.exists():
        target = _free_legacy_path(path.parent)
        path.rename(target)
        logger.info("legacy database renamed aside: %s -> %s", path, target)
    db.init_db(path)
    return path


def _free_legacy_path(parent: Path) -> Path:
    base = parent / "slopclanker-legacy.db"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = parent / f"slopclanker-legacy-{n}.db"
        if not candidate.exists():
            return candidate
        n += 1

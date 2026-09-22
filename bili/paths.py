from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LIBRARY_DIR = DATA_DIR / "library"
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "app.db"
JOBS_DIR = DATA_DIR / "jobs"

# Analysis-grade relational dataset, kept separate from the operational app.db so
# that rebuilding or handing off the corpus never touches UI/job state.
CORPUS_DB_PATH = DATA_DIR / "corpus.db"
EXPORT_DIR = DATA_DIR / "exports"
PROFILES_DIR = DATA_DIR / "academic_profiles"


def ensure_dirs() -> None:
    for path in (DATA_DIR, LIBRARY_DIR, JOBS_DIR, EXPORT_DIR, PROFILES_DIR):
        path.mkdir(parents=True, exist_ok=True)

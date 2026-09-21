# Plan

1. `uvx vulture src --min-confidence 60` to list candidates.
2. `grep -rnI` each candidate across the whole repo; drop anything with
   a reference, a framework hook, or a re-export contract (see spec).
3. Delete the survivors and any import left unused by the deletion.
4. `isort --check` / `pyink --check` on touched files.
5. `pip install -e ".[dev,streamlit]"` then `pytest --tb=short -q`
   (same as `.github/workflows/ci.yml`).

## Result

- 7 files, 59 lines deleted, 0 added under `src/`.
- `pytest --tb=short -q`: 5334 passed, 66 skipped (Python 3.12).
- `vulture src --min-confidence 80` after the change reports only the
  three kept items listed in the spec.

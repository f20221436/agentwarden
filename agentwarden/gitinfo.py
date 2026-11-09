"""Git provenance for runs (commit SHA + dirty flag)."""

from __future__ import annotations

import subprocess


def git_info() -> tuple[str | None, bool]:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
                             timeout=5).stdout.strip()
        # Generated artefacts (RESULTS.md, raw run JSON) do not make the *code* dirty.
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", ".",
                                     ":!docs/RESULTS.md", ":!evals/results"], capture_output=True,
                                    text=True, check=True, timeout=5).stdout.strip())
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return None, False

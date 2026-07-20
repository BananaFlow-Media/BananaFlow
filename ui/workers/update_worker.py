"""
ui/workers/update_worker.py  –  App-release + component update check worker
============================================================================
One background thread that can run either or both of the app's two
update checks and hand the combined result back to the UI thread:

  * App releases   — core.update_checker (GitHub Releases API)
  * Components     — core.component_updates (PyPI JSON API, yt-dlp & co.)

Both underlying checkers are guaranteed never to raise — failures are
expressed in their result objects — so run() needs no try/except and a
flaky network can never produce a startup error dialog.

Used from two places:
  * AppWindow startup (check_app=True, check_components=True): results
    are filtered through core.update_state.UpdateStateStore and only
    non-dismissed, non-snoozed updates produce a notification.
  * The Settings panel's "Check for app updates" / "Check for component
    updates" buttons (one flag each): results are always reported,
    including "up to date" and "check failed".

Signal summary
--------------
results_ready(UpdateCheckResults)
    Emitted exactly once per run with whatever was requested.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core.component_updates import ComponentUpdateChecker, ComponentUpdateReport
from core.update_checker import UpdateChecker, UpdateCheckOutcome


@dataclass
class UpdateCheckResults:
    """Combined result of one worker run. A field is None when that
    check was not requested for this run."""
    app:        Optional[UpdateCheckOutcome] = None
    components: Optional[ComponentUpdateReport] = None


class UpdateWorker(QThread):
    """
    One-shot background update checker.

    Parameters
    ----------
    repo_owner          : GitHub org / user that owns the repository.
    repo_name           : Repository name on GitHub.
    include_prereleases : When True, pre-release versions are also considered.
    check_app           : Query GitHub Releases for a newer BananaFlow build.
    check_components    : Query PyPI for newer yt-dlp / yt-dlp-ejs releases.
    parent              : Optional Qt parent object.
    """

    results_ready = Signal(object)   # UpdateCheckResults

    def __init__(
        self,
        repo_owner:          str  = "BananaFlow-Media",
        repo_name:           str  = "BananaFlow",
        include_prereleases: bool = False,
        check_app:           bool = True,
        check_components:    bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._checker = UpdateChecker(
            repo_owner=repo_owner,
            repo_name=repo_name,
        )
        self._include_prereleases = include_prereleases
        self._check_app = check_app
        self._check_components = check_components

    def run(self) -> None:
        results = UpdateCheckResults()
        if self._check_app:
            results.app = self._checker.check_detailed(
                include_prereleases=self._include_prereleases,
            )
        if self._check_components:
            results.components = ComponentUpdateChecker().check()
        self.results_ready.emit(results)

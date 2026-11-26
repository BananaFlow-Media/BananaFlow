"""
ui/workers/update_worker.py  –  App-release + component update check worker
============================================================================
One background thread runs the requested update checks and hands their
combined result back to the UI thread:

  * App releases   — core.update_checker (GitHub Releases API)
  * Components     — core.component_updates (official GitHub channel or PyPI)
  * Packaged tools and maintenance — official assets for this app build

Check failures are
expressed in their result objects — so run() needs no try/except and a
flaky network can never produce a startup error dialog.

Used from two places:
  * AppWindow startup (check_app=True, check_components=True): results
    are filtered through core.update_state.UpdateStateStore and only
    non-dismissed, non-snoozed updates produce a notification.
  * Settings can check the app release alone or check all channels in a
    packaged build. Source mode can check the PyPI components separately.
    Results include "up to date" and "check failed".

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
    maintenance: Optional[UpdateCheckOutcome] = None


class UpdateWorker(QThread):
    """
    One-shot background update checker.

    Parameters
    ----------
    repo_owner          : GitHub org / user that owns the repository.
    repo_name           : Repository name on GitHub.
    include_prereleases : When True, pre-release versions are also considered.
    check_app           : Query GitHub Releases for a newer BananaFlow build.
    check_components    : Check components and, for packaged builds, maintenance.
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
        if self.isInterruptionRequested():
            return
        if self._check_app:
            results.app = self._checker.check_detailed(
                include_prereleases=self._include_prereleases,
            )
        if self.isInterruptionRequested():
            return
        if self._check_components:
            results.components = ComponentUpdateChecker().check()
        if self.isInterruptionRequested():
            return
        if self._check_components:
            from utils.paths import is_frozen
            if is_frozen():
                from core.app_updates import check_app_maintenance
                from core.tool_updates import check_tools
                results.components.components.extend(check_tools(timeout=8))
                if self.isInterruptionRequested():
                    return
                results.maintenance = check_app_maintenance()
        if not self.isInterruptionRequested():
            self.results_ready.emit(results)

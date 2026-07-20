"""
core/services.py  –  Lightweight service container for BananaFlow
=========================================================================
Holds all shared backend singletons in one object that is constructed in
main.py and passed to AppWindow (and eventually to a CLI entry point).

Benefits
--------
* AppWindow no longer constructs its own DownloadEngine — it receives one.
* Integration tests can create a ServiceContainer with mock/stub services
  and pass it to AppWindow (or to individual panels) without launching a
  full application.
* A future CLI entry point can reuse the exact same container.

Usage
-----
    # main.py
    svc = ServiceContainer.create_default(cfg)
    window = AppWindow(config=cfg, services=svc)

    # tests
    svc = ServiceContainer(
        config=mock_cfg,
        db=HistoryDB(":memory:"),
        engine=FakeEngine(),
    )

Zero GUI imports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from config import AppConfig
from core.history_db import HistoryDB
from core.downloader import DownloadEngine

logger = logging.getLogger(__name__)


@dataclass
class ServiceContainer:
    """
    Holds references to all shared backend services.

    All fields are public and mutable so tests can swap them freely.
    The ``create_default`` classmethod builds the production configuration.
    """

    config: AppConfig
    db:     HistoryDB
    engine: DownloadEngine

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create_default(cls, config: Optional[AppConfig] = None) -> "ServiceContainer":
        """
        Build the standard production service container.

        Parameters
        ----------
        config : An existing AppConfig, or None to create a fresh one.
        """
        cfg = config or AppConfig()
        db  = HistoryDB(cfg.resolved_history_db_path())
        eng = DownloadEngine()

        logger.info(
            "[Services] Created default container (db=%s, %d records)",
            cfg.resolved_history_db_path(), db.count(),
        )
        return cls(config=cfg, db=db, engine=eng)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Shut down all closeable services.  Safe to call multiple times."""
        try:
            self.db.close()
            logger.debug("[Services] Database closed")
        except Exception:
            pass

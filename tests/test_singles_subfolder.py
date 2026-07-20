from unittest.mock import MagicMock
import pytest
from PySide6.QtCore import QObject

from ui.controllers.download_controller import DownloadController
from config import AppConfig
from core.downloader import DownloadEngine


class DummyParent(QObject):
    pass


@pytest.fixture
def controller():
    cfg = AppConfig()
    engine = DownloadEngine()
    return DownloadController(config=cfg, engine=engine, parent=DummyParent())


def test_get_dynamic_folder_singles_subfolder_enabled(controller):
    """Test dynamic folder calculation when singles subfolder setting is enabled (default)."""
    controller._cfg.singles_subfolder = True

    # Mock a card representing a single from an artist discography
    card = MagicMock()
    card.parent_artist = "Idan Raichel"
    card.artist = "Idan Raichel"
    card.album = ""
    card.release_type = "single"

    # Under discography structure: Artist / Category (Singles & EPs)
    folder = controller._get_dynamic_folder(card, fallback=None, is_discography=True)
    assert "Idan Raichel" in folder
    # Should include the localized "Singles & EPs" or "סינגלים ו-EP" category folder
    assert "EP" in folder or "ep" in folder.lower() or "מיני" in folder


def test_get_dynamic_folder_singles_subfolder_disabled(controller):
    """Test dynamic folder calculation when singles subfolder setting is disabled."""
    controller._cfg.singles_subfolder = False

    card = MagicMock()
    card.parent_artist = "Idan Raichel"
    card.artist = "Idan Raichel"
    card.album = ""
    card.release_type = "single"

    # Should omit the category folder and return just the artist folder
    folder = controller._get_dynamic_folder(card, fallback=None, is_discography=True)
    assert folder == "Idan Raichel"


def test_get_dynamic_folder_albums_unaffected(controller):
    """Test that album structures are unaffected by the singles subfolder setting."""
    controller._cfg.singles_subfolder = False

    card = MagicMock()
    card.parent_artist = "Idan Raichel"
    card.artist = "Idan Raichel"
    card.album = "Project Album"
    card.release_type = "album"

    folder = controller._get_dynamic_folder(card, fallback=None, is_discography=True)
    assert "Idan Raichel" in folder
    # Albums should still have their category and album folder
    assert "אלבומים" in folder or "Album" in folder
    assert "Project Album" in folder


def test_playlist_subfolders_disabled_yields_empty_string(controller):
    """Test that if playlist_subfolders is False, track_playlist_name fallback is empty string."""
    controller._cfg.playlist_subfolders = False

    card = MagicMock()
    card.parent_artist = "Idan Raichel"
    card.artist = "Idan Raichel"
    card.album = "Project Album"
    card.release_type = "album"

    # Simulate download loop logic:
    track_playlist_name = ""
    folder = controller._get_dynamic_folder(card, fallback=track_playlist_name, is_discography=True)
    assert folder == ""

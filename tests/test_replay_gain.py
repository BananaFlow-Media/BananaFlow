import sys
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def mock_dependencies():
    # Save original modules
    orig = {
        'soundfile': sys.modules.get('soundfile'),
        'pyloudnorm': sys.modules.get('pyloudnorm'),
        'numpy': sys.modules.get('numpy'),
    }

    # Inject mocks
    sys.modules['soundfile'] = MagicMock()
    sys.modules['pyloudnorm'] = MagicMock()
    sys.modules['numpy'] = MagicMock()

    yield

    # Restore original modules
    for name, value in orig.items():
        if value is not None:
            sys.modules[name] = value
        else:
            sys.modules.pop(name, None)


from core.replay_gain import _analyse_with_pyloudnorm, _analysis_lock


@patch("pathlib.Path.stat")
def test_replay_gain_large_file_skipped(mock_stat):
    """Test that a file larger than 150MB is skipped to prevent memory spikes."""
    mock_file = Path("large_mix.mp3")

    # 151 MB in bytes
    mock_stat.return_value.st_size = 151 * 1024 * 1024

    # Run the analysis
    result = _analyse_with_pyloudnorm(mock_file)

    # Should skip analysis and return False
    assert result is False


@patch("soundfile.read")
@patch("pyloudnorm.Meter")
@patch("core.replay_gain._write_tags")
@patch("pathlib.Path.stat")
def test_replay_gain_lock_concurrency(mock_stat, mock_write_tags, mock_meter, mock_sf_read):
    """Test that the global threading lock is acquired during pyloudnorm analysis."""
    mock_file = Path("normal_track.mp3")
    mock_stat.return_value.st_size = 10 * 1024 * 1024  # 10 MB

    # Setup soundfile mock data
    mock_sf_read.return_value = (MagicMock(), 44100)

    # Setup pyloudnorm meter mock
    mock_meter_instance = MagicMock()
    mock_meter_instance.integrated_loudness.return_value = -14.0
    mock_meter.return_value = mock_meter_instance

    # We want to verify that when sf.read runs, the global lock is locked
    def assert_locked(*args, **kwargs):
        assert _analysis_lock.locked() is True
        return MagicMock(), 44100

    mock_sf_read.side_effect = assert_locked

    # Assert lock is currently unlocked
    assert _analysis_lock.locked() is False

    result = _analyse_with_pyloudnorm(mock_file)

    assert result is True
    assert _analysis_lock.locked() is False
    assert mock_sf_read.call_count == 1

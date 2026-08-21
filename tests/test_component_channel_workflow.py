"""Static policy gates for independently published downloader components."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "component-channel.yml"
BUILDER = ROOT / "scripts" / "build_component_channel.py"


def test_channel_is_scheduled_but_only_reviewed_events_publish():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "schedule:" in text and "cron:" in text
    assert "if: github.event_name != 'schedule'" in text
    assert "branches: [main]" in text
    assert "'v[0-9]+.[0-9]+.[0-9]+'" in text
    assert "requirements.txt" in text and "constraints-release.txt" in text


def test_channel_never_authors_an_unreviewed_change():
    text = WORKFLOW.read_text(encoding="utf-8")
    forbidden = ("git commit", "git push", "gh pr create", "peter-evans/create-pull-request")
    assert not any(command in text for command in forbidden)


def test_bundle_is_uploaded_before_authenticated_manifest_is_replaced():
    text = WORKFLOW.read_text(encoding="utf-8")
    publish = text.split("Create or update the official pre-release channel", 1)[1]
    assert publish.index('"$bundle" --clobber') < publish.index(
        "bananaflow-components.json --clobber"
    )
    assert "permissions:\n      contents: write" in text


def test_builder_contains_only_the_two_reviewed_distributions():
    text = BUILDER.read_text(encoding="utf-8")
    assert 'PACKAGES = ("yt-dlp", "yt-dlp-ejs")' in text
    assert "ZipInfo" in text and "sha256" in text


def test_builder_runs_when_invoked_by_path_outside_the_repository(tmp_path):
    output = tmp_path / "channel"
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (output / "bananaflow-components.json").is_file()

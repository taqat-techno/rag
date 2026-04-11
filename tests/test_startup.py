"""Tests for Windows startup integration (Startup folder).

Uses temp directories to avoid modifying the real Startup folder.
"""

import sys
from unittest.mock import patch
from pathlib import Path

import pytest

from ragtools.config import Settings
from ragtools.service import startup


# --- Task name ---

def test_task_name():
    assert startup.TASK_NAME == "RAGTools Service"


# --- Platform check ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_check_windows_passes():
    startup._check_windows()  # Should not raise


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows only")
def test_check_windows_raises_on_non_windows():
    with pytest.raises(RuntimeError, match="only available on Windows"):
        startup._check_windows()


# --- Install task ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_install_task_success(tmp_path):
    """Install creates a VBScript in the startup folder."""
    settings = Settings()
    with patch.object(startup, "_get_startup_folder", return_value=tmp_path):
        result = startup.install_task(settings, delay_seconds=30)
    assert result is True
    script = tmp_path / startup.STARTUP_FILENAME
    assert script.exists()
    content = script.read_text()
    assert "RAGTools Auto-Start" in content
    assert "WScript.Sleep 30000" in content


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_install_task_custom_delay(tmp_path):
    """Install respects custom delay."""
    settings = Settings()
    with patch.object(startup, "_get_startup_folder", return_value=tmp_path):
        startup.install_task(settings, delay_seconds=60)
    script = tmp_path / startup.STARTUP_FILENAME
    content = script.read_text()
    assert "WScript.Sleep 60000" in content


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_install_task_overwrites(tmp_path):
    """Install overwrites existing script."""
    settings = Settings()
    script = tmp_path / startup.STARTUP_FILENAME
    script.write_text("old content")
    with patch.object(startup, "_get_startup_folder", return_value=tmp_path):
        startup.install_task(settings, delay_seconds=30)
    content = script.read_text()
    assert "RAGTools Auto-Start" in content
    assert "old content" not in content


# --- Uninstall task ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_uninstall_task_success(tmp_path):
    """Uninstall removes the startup script."""
    script = tmp_path / startup.STARTUP_FILENAME
    script.write_text("test")
    with patch.object(startup, "_get_startup_folder", return_value=tmp_path):
        with patch.object(startup, "_get_startup_script_path", return_value=script):
            result = startup.uninstall_task()
    assert result is True
    assert not script.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_uninstall_task_not_found(tmp_path):
    """Uninstall succeeds even if script doesn't exist."""
    script = tmp_path / startup.STARTUP_FILENAME
    with patch.object(startup, "_get_startup_folder", return_value=tmp_path):
        with patch.object(startup, "_get_startup_script_path", return_value=script):
            result = startup.uninstall_task()
    assert result is True


# --- Is task installed ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_is_task_installed_yes(tmp_path):
    """Returns True when startup script exists."""
    script = tmp_path / startup.STARTUP_FILENAME
    script.write_text("test")
    with patch.object(startup, "_get_startup_script_path", return_value=script):
        assert startup.is_task_installed() is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_is_task_installed_no(tmp_path):
    """Returns False when startup script doesn't exist."""
    script = tmp_path / startup.STARTUP_FILENAME
    with patch.object(startup, "_get_startup_script_path", return_value=script):
        assert startup.is_task_installed() is False


def test_is_task_installed_non_windows():
    """On non-Windows, should return False."""
    if sys.platform != "win32":
        assert startup.is_task_installed() is False


# --- Config fields ---

def test_settings_has_startup_fields():
    settings = Settings()
    assert hasattr(settings, "startup_enabled")
    assert hasattr(settings, "startup_delay")
    assert hasattr(settings, "startup_open_browser")
    assert settings.startup_delay == 30
    assert settings.startup_open_browser is False

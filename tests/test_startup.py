"""Tests for Windows startup integration (Task Scheduler).

Uses mocks for schtasks — no real task creation in CI.
"""

import sys
from unittest.mock import patch, MagicMock

import pytest

from ragtools.config import Settings
from ragtools.service import startup


# --- Task name ---

def test_task_name():
    assert startup.TASK_NAME == "RAGTools Service"


# --- Delay formatting ---

def test_delay_str_30_seconds():
    assert startup._delay_str(30) == "0000:01"  # Min 1 minute


def test_delay_str_60_seconds():
    assert startup._delay_str(60) == "0000:01"


def test_delay_str_120_seconds():
    assert startup._delay_str(120) == "0000:02"


def test_delay_str_3600_seconds():
    assert startup._delay_str(3600) == "0001:00"


# --- Command building ---

def test_build_task_command():
    settings = Settings()
    cmd = startup._build_task_command(settings)
    assert "ragtools.service.run" in cmd
    assert "--from-scheduler" in cmd


# --- Platform check ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_check_windows_passes():
    startup._check_windows()  # Should not raise


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows only")
def test_check_windows_raises_on_non_windows():
    with pytest.raises(RuntimeError, match="only available on Windows"):
        startup._check_windows()


# --- Install task (mocked) ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_install_task_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="SUCCESS", stderr="")
    settings = Settings()
    result = startup.install_task(settings, delay_seconds=30)
    assert result is True
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "schtasks" in cmd
    assert "/create" in cmd
    assert startup.TASK_NAME in cmd


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_install_task_failure(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Access denied")
    settings = Settings()
    with pytest.raises(RuntimeError, match="schtasks failed"):
        startup.install_task(settings, delay_seconds=30)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_install_task_custom_delay(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="SUCCESS", stderr="")
    settings = Settings()
    startup.install_task(settings, delay_seconds=300)
    cmd = mock_run.call_args[0][0]
    assert "0000:05" in cmd  # 300s = 5min


# --- Uninstall task (mocked) ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_uninstall_task_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="SUCCESS", stderr="")
    result = startup.uninstall_task()
    assert result is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_uninstall_task_not_found(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ERROR: cannot find the file")
    result = startup.uninstall_task()
    assert result is True  # Not found = success (idempotent)


# --- Is task installed (mocked) ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_is_task_installed_yes(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert startup.is_task_installed() is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
@patch("subprocess.run")
def test_is_task_installed_no(mock_run):
    mock_run.return_value = MagicMock(returncode=1)
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

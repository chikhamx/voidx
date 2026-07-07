"""Tests for RetryConfig model and SettingsRetryMixin."""

import pytest
from pathlib import Path

from voidx.config import RetryConfig, Settings


def _set_home(monkeypatch, path: Path) -> None:
    monkeypatch.setattr("voidx.config.settings._settings_home", lambda: path)


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    yield


class TestRetryConfig:
    def test_defaults(self):
        rc = RetryConfig()
        assert rc.max_attempts == 3
        assert rc.base_delay == 1.0
        assert rc.max_delay == 10.0
        assert rc.jitter is True

    def test_validation_max_attempts(self):
        with pytest.raises(Exception):
            RetryConfig(max_attempts=0)
        with pytest.raises(Exception):
            RetryConfig(max_attempts=11)

    def test_validation_base_delay(self):
        with pytest.raises(Exception):
            RetryConfig(base_delay=-1.0)
        with pytest.raises(Exception):
            RetryConfig(base_delay=61.0)

    def test_validation_max_delay(self):
        with pytest.raises(Exception):
            RetryConfig(max_delay=-1.0)
        with pytest.raises(Exception):
            RetryConfig(max_delay=121.0)

    def test_custom_values(self):
        rc = RetryConfig(max_attempts=5, base_delay=2.0, max_delay=30.0, jitter=False)
        assert rc.max_attempts == 5
        assert rc.base_delay == 2.0
        assert rc.max_delay == 30.0
        assert rc.jitter is False


class TestSettingsRetryMixin:
    def test_get_retry_config_default(self, tmp_path):
        settings = Settings(str(tmp_path))
        rc = settings.get_retry_config()
        assert rc.max_attempts == 3
        assert rc.base_delay == 1.0
        assert rc.max_delay == 10.0
        assert rc.jitter is True

    def test_get_retry_config_from_settings(self, tmp_path):
        settings = Settings(str(tmp_path))
        settings._set_setting("retry", {
            "max_attempts": 5,
            "base_delay": 2.0,
            "max_delay": 30.0,
            "jitter": False,
        })
        rc = settings.get_retry_config()
        assert rc.max_attempts == 5
        assert rc.base_delay == 2.0
        assert rc.max_delay == 30.0
        assert rc.jitter is False

    def test_get_retry_config_invalid_falls_back_to_default(self, tmp_path):
        settings = Settings(str(tmp_path))
        settings._set_setting("retry", {"max_attempts": "not-a-number"})
        rc = settings.get_retry_config()
        assert rc.max_attempts == 3
        assert rc.base_delay == 1.0

    def test_get_retry_config_partial_falls_back_to_default(self, tmp_path):
        settings = Settings(str(tmp_path))
        settings._set_setting("retry", {"max_attempts": 5})
        rc = settings.get_retry_config()
        assert rc.max_attempts == 5
        assert rc.base_delay == 1.0
        assert rc.max_delay == 10.0
        assert rc.jitter is True

    def test_retry_is_global_key(self, tmp_path):
        settings = Settings(str(tmp_path))
        settings._set_setting("retry", {"max_attempts": 5})
        assert "retry" in settings._effective_data()

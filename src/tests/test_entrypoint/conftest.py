from __future__ import annotations

import pytest

from voidx.llm import catalog


@pytest.fixture(autouse=True)
def restore_catalog_settings_binding():
    previous = catalog._settings
    yield
    catalog.bind_settings(previous)

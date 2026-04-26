from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_maurice_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAURICE_HOME", str(tmp_path / ".maurice"))

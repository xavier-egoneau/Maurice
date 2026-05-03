from __future__ import annotations

from maurice.host import repl
from maurice.host.project_registry import list_machine_projects
from maurice.host.project import ensure_maurice_dir
from maurice.kernel.session import SessionStore


def test_session_rows_include_current_unsaved_session(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.create("main", session_id="existing")

    rows = repl._session_rows(store.list("main"), "draft")

    assert rows[0] == ("draft", 0, 0, "nouvelle")
    assert any(row[0] == "existing" for row in rows)


def test_switch_session_creates_named_session(tmp_path) -> None:
    ensure_maurice_dir(tmp_path)

    switched = repl._switch_session(tmp_path, "/session refacto", "default")

    assert switched == "refacto"
    assert SessionStore(tmp_path / ".maurice" / "sessions").load("main", "refacto").id == "refacto"


def test_stream_turn_does_not_emit_ansi_rerender(capsys) -> None:
    class FakeClient:
        def run_turn(self, *_args, **_kwargs):
            yield {"type": "text_delta", "delta": "ligne tres longue " * 10}
            yield {"type": "done", "input_tokens": 0, "output_tokens": 0}

    repl._stream_turn(FakeClient(), "hello", "default")

    output = capsys.readouterr().out
    assert "ligne tres longue" in output
    assert "\033[" not in output


def test_context_bar_is_clamped_to_terminal_width(capsys) -> None:
    repl._print_context_bar(2_000_000, 66_631)

    output = capsys.readouterr().out
    assert "2,066,631 / 250,000 tokens" in output
    assert output.count("█") <= int(repl._console.width * 0.8)


def test_welcome_tips_do_not_expose_setup_command() -> None:
    assert all(command != "/setup" for command, _description in repl._TIPS)


def test_run_repl_records_opened_project(tmp_path, monkeypatch) -> None:
    class FakeClient:
        def __init__(self, _project_root):
            pass

        def ensure_running(self):
            pass

        def connect(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(repl, "MauriceClient", FakeClient)
    monkeypatch.setattr(repl, "_welcome", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl._console, "input", lambda *_args, **_kwargs: (_ for _ in ()).throw(EOFError()))

    repl.run_repl(tmp_path, session_id="default")

    assert list_machine_projects()[0]["path"] == str(tmp_path.resolve())

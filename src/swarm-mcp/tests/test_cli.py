import io

from mathai_swarm_mcp.cli import main
from mathai_swarm_mcp.keystore import MemoryStore
from tests.conftest import make_client


def test_login_prints_consent_not_tokens():
    broker, store, _ = make_client()
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["login", "--origin", "https://a2a.mathai.com.br", "--principal", "advisor-01"],
        store=store,
        transport=broker.transport(),
        stdout=stdout,
        stderr=stderr,
        sleeper=lambda _: None,
    )
    assert code == 0
    text = stdout.getvalue()
    err = stderr.getvalue()
    combined = text + err
    assert "https://github.com/login/device" in text
    assert "WD4X-T7NK" in text
    assert "device_" not in combined
    assert "access_" not in combined
    assert "refresh_" not in combined
    assert '"d"' not in combined
    assert store.load("https://a2a.mathai.com.br", "advisor-01").refresh_token.startswith("refresh_")


def test_show_key_and_logout():
    broker, store, _ = make_client()
    stdout = io.StringIO()
    code = main(
        ["show-key", "--origin", "https://a2a.mathai.com.br", "--principal", "advisor-01"],
        store=store,
        stdout=stdout,
    )
    assert code == 0
    body = stdout.getvalue()
    assert "jkt" in body and "refresh_" not in body and '"d"' not in body
    main(
        ["login", "--origin", "https://a2a.mathai.com.br", "--principal", "advisor-01"],
        store=store,
        transport=broker.transport(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        sleeper=lambda _: None,
    )
    code = main(
        ["logout", "--origin", "https://a2a.mathai.com.br", "--principal", "advisor-01"],
        store=store,
        transport=broker.transport(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 0
    assert store.load("https://a2a.mathai.com.br", "advisor-01") is None


def test_help_lists_user_commands_not_policy_admin(capsys):
    code = main(["--help"])
    assert code == 0
    text = capsys.readouterr().out
    assert "login" in text and "logout" in text and "serve" in text and "show-key" in text
    assert "grant" not in text and "principal add" not in text

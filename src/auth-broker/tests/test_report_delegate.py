import builtins

from app.cli import main


def test_report_missing_optional_package(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "swarm_reports.cli" or (
            fromlist and "cli" in fromlist and name == "swarm_reports"
        ):
            raise ImportError("no reports package")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    code = main(["report", "morning"])
    assert code == 2

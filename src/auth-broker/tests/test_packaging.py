"""Check the installed package exposes the documented development extra."""

from importlib.metadata import metadata


def test_dev_extra_installs_pytest():
    package = metadata("agent-pairing-broker")
    assert "dev" in package.get_all("Provides-Extra", [])
    assert any(
        requirement.startswith("pytest") and 'extra == "dev"' in requirement
        for requirement in package.get_all("Requires-Dist", [])
    )

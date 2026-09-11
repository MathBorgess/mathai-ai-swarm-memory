import subprocess
import sys
from importlib.metadata import metadata
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_and_entrypoints():
    package = metadata("mathai-swarm-mcp")
    assert package["Name"] == "mathai-swarm-mcp"
    assert "dev" in package.get_all("Provides-Extra", [])


def test_help_entrypoints():
    script = subprocess.check_output(["mathai-swarm-mcp", "--help"], text=True)
    module = subprocess.check_output([sys.executable, "-m", "mathai_swarm_mcp", "--help"], text=True)
    for text in (script, module):
        assert "login" in text
        assert "serve" in text
        assert "logout" in text
        assert "show-key" in text


def test_clean_venv_install_and_help(tmp_path):
    venv = tmp_path / "venv"
    subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    pip = venv / "bin" / "pip"
    subprocess.check_call([str(pip), "install", "--upgrade", "pip"], stdout=subprocess.DEVNULL)
    subprocess.check_call([str(pip), "install", str(PACKAGE_ROOT)])
    binary = venv / "bin" / "mathai-swarm-mcp"
    script = subprocess.check_output([str(binary), "--help"], text=True)
    module = subprocess.check_output([str(venv / "bin" / "python"), "-m", "mathai_swarm_mcp", "--help"], text=True)
    assert "login" in script and "serve" in module
    assert "access_token" not in script
    assert "Authorization:" not in script

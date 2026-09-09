from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "setup-vps.sh"


def test_setup_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_setup_script_exposes_safe_help_without_loading_configuration():
    result = subprocess.run([str(SCRIPT), "--help"], check=True, capture_output=True, text=True)

    assert "Usage: setup-vps.sh /absolute/path/to/auth-broker.env" in result.stdout
    assert "secrets" not in result.stdout.lower()

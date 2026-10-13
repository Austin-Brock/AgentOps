"""Configuration and credential loading.

The .env loading path is easy to get silently wrong — a key in the file that the
provider SDK never sees looks identical to a missing key. These assert the
contract that `.env.example` advertises.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.config import REPO_ROOT, Settings


def test_defaults_match_the_documented_business_rules() -> None:
    settings = Settings()
    assert settings.refund_window_days == 30
    assert settings.refund_auto_approve_max_cents == 5_000
    assert settings.refund_manager_approval_max_cents == 20_000


def test_repo_root_resolves_to_the_checkout() -> None:
    assert (REPO_ROOT / "data" / "seed" / "orders.json").exists()
    assert (REPO_ROOT / ".env.example").exists()


def test_dotenv_populates_the_environment_the_sdk_reads(tmp_path: Path) -> None:
    """A key in .env must reach os.environ, because that is where the SDK looks."""
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-not-a-real-key\n")

    # A subprocess so the real environment is never mutated by the test.
    code = (
        "import os;"
        "from dotenv import load_dotenv;"
        f"load_dotenv({str(env_file)!r}, override=False);"
        "print(os.environ.get('ANTHROPIC_API_KEY'))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "sk-ant-not-a-real-key"


def test_exported_key_wins_over_dotenv(tmp_path: Path) -> None:
    """override=False: a key already in the shell is not clobbered by the file."""
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=from-file\n")

    code = (
        "import os;"
        "from dotenv import load_dotenv;"
        f"load_dotenv({str(env_file)!r}, override=False);"
        "print(os.environ['ANTHROPIC_API_KEY'])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"ANTHROPIC_API_KEY": "from-shell", "PATH": "/usr/bin:/bin"},
    )
    assert out.stdout.strip() == "from-shell"

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_ai_support_classifier_and_clarification_have_no_unused_symbols() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "F401,F841",
            "codepilot/ai_support/classifier.py",
            "codepilot/ai_support/clarification_protocol.py",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert result.returncode == 0, result.stdout

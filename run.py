"""Start the local LangGraph Runtime from an IDE debugger."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langgraph_cli.cli import cli

PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    os.chdir(PROJECT_ROOT)
    load_dotenv(PROJECT_ROOT / ".env")
    sys.argv = [
        sys.argv[0],
        "dev",
        "--server-log-level",
        "debug",
        "--no-browser",
        *sys.argv[1:],
    ]
    cli()


if __name__ == "__main__":
    main()

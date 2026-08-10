"""Single project launcher.

Examples:
    python main.py serve --port 8765
    python main.py download --input example_doi_list.txt --concurrency 2
    python main.py agent --input search_batch.json --base-url http://127.0.0.1:8765
"""

from __future__ import annotations

import sys


def main():
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print(
            "Usage:\n"
            "  python main.py serve [paper-tool-server args]\n"
            "  python main.py download [paper-tool args]\n"
            "  python main.py agent [paper-tool-agent args]\n"
        )
        return 0

    command = sys.argv.pop(1)
    if command == "serve":
        from paper_tool.api import main as entry
    elif command == "download":
        from paper_tool.cli import main as entry
    elif command == "agent":
        from paper_tool.agent_client import main as entry
    else:
        raise SystemExit(f"Unknown command: {command}")
    return entry()


if __name__ == "__main__":
    raise SystemExit(main())

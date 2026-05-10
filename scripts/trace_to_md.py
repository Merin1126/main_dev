from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.trace_report import convert_current_trace_to_md


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert latest Gemini trace JSONL into Markdown report.")
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="Project root path (default: repository root).",
    )
    args = parser.parse_args()

    out = convert_current_trace_to_md(args.project_root)
    print(f"Trace report generated: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

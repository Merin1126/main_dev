from __future__ import annotations

import json
import os
import sys


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    from services.scraper_health_check_service import ScraperHealthCheckService

    result = ScraperHealthCheckService(project_root=root).run()
    print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

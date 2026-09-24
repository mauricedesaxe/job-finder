from __future__ import annotations

from job_finder.config import LangfuseSettings
from job_finder.projections.smoke import run_live_smoke


def main() -> int:
    settings = LangfuseSettings.from_environment()
    run_live_smoke(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

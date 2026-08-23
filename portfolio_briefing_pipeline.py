#!/usr/bin/env python3
"""
Compatibility entry point for the portfolio briefing pipeline.

The implementation lives in the ``portfolio_briefing`` package, split by the
main pipeline stages: config/models/utils, prices, holdings, attribution, news,
Gemini, and orchestration.
"""

from __future__ import annotations

import sys

from portfolio_briefing.pipeline import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

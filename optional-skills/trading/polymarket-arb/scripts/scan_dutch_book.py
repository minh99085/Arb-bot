#!/usr/bin/env python3
"""Hermes cron entrypoint — Phase 1 study scan (no LLM, no trading)."""

from arb.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["scan", "--study", "--top", "5"]))

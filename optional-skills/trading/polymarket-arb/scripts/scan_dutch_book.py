#!/usr/bin/env python3
"""Hermes cron entrypoint — scan Polymarket for Dutch-book arbs without LLM cost."""

from arb.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["scan", "--top", "5"]))

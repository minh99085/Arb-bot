#!/usr/bin/env python3
"""Hermes cron entrypoint — Phase 4 daily postmortem (deterministic, no LLM)."""

from arb.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["postmortem", "--days", "7"]))

"""CLI package entry: python -m arb …"""

from __future__ import annotations

import sys

from arb.cli import main as cli_main


def main(argv: list[str] | None = None) -> int:
    return cli_main(argv)


def worker_main(argv: list[str] | None = None) -> int:
    """Console script: polymarket-arb-worker → `worker …`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"run", "once", "status"}:
        args = ["run", *args]
    return cli_main(["worker", *args])


if __name__ == "__main__":
    raise SystemExit(main())

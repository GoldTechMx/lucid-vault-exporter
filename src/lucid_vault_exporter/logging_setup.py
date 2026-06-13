"""Rich console logging. Tokens must never appear in logs - the client guarantees that;
this module just formats."""

from __future__ import annotations

import logging

from rich.logging import RichHandler


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=False, show_path=False)],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

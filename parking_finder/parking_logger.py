"""This file contains the implementation of the parking logger."""

import logging
import sys
from typing import ClassVar

from typing_extensions import override


class ColorFormatter(logging.Formatter):
    """Formatter for colored logging output."""

    COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[41m",  # Red background
    }
    RESET: ClassVar[str] = "\033[0m"

    @override
    def format(self, record: logging.LogRecord) -> str:
        levelname = record.levelname

        # Pad before coloring
        padded = f"{levelname:<8}"

        # Wrap only the visible part in color
        if levelname in self.COLORS:
            colored = f"{self.COLORS[levelname]}{padded}{self.RESET}"
            record.levelname = colored
        else:
            record.levelname = padded

        return super().format(record)


def setup_logging(level: str) -> None:
    """Configure the logger.

    Sets up logging to stderr with a custom formatter. Applies the log level
    globally using ``logging.basicConfig``. Subsequent calls have no effect
    if logging is already configured.

    Args:
        level (str): The log level to set. Defaults to "INFO".
    """
    # Skip reconfiguration if handlers already exist
    if logging.getLogger().handlers:
        return

    levels = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
        "NOTSET": logging.NOTSET,
    }

    level_upper = level.upper()

    # If the log level is missing or empty, disable all logging
    if not level_upper:
        logging.disable(logging.CRITICAL)
        return

    # Validate the level name and get the corresponding logging constant
    log_level = levels.get(level_upper)

    if log_level is None:
        # Invalid log level specified, print an error and exit.
        print(f"invalid log level '{level}' specified")
        sys.exit(1)

    # Stream logs to stderr and set formatter
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))

    # Configure logging and output log level
    logging.basicConfig(level=log_level, handlers=[handler])
    logging.getLogger().debug(
        "setting log level to %s",
        logging.getLevelName(log_level),
    )

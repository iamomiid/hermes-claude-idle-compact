"""Run synthetic tests in a temporary data home, with outbound sockets disabled."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def refuse_network(*args, **kwargs):
    raise AssertionError("Tests cannot open network connections")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-hermes", action="store_true", help="Fail if host integration tests are unavailable/skipped")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="claude-idle-check-") as tmp, \
            patch.dict(os.environ, {"HERMES_HOME": tmp}), \
            patch.object(socket, "create_connection", refuse_network), \
            patch.object(socket.socket, "connect", refuse_network), \
            patch.object(socket.socket, "connect_ex", refuse_network):
        if args.require_hermes:
            try:
                import agent.context_engine  # noqa: F401
                import anthropic  # noqa: F401
            except ImportError:
                parser.error("Use a Hermes runtime with its Anthropic extra for --require-hermes")
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if args.require_hermes and result.skipped:
            print("Host integration tests were skipped; compatibility is unverified.", file=sys.stderr)
            return 1
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

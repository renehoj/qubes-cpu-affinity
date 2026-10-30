"""``qubes-vcpu <qube>``: the vCPU window on its own."""

from __future__ import annotations

import argparse
import logging
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk  # noqa: E402,F401  pylint: disable=unused-import

from .backend import KINDS, create_backend  # noqa: E402
from .model import DEFAULT_CONFIG  # noqa: E402
from .window import WINDOW_ALPHA, VcpuWindow  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qubes-vcpu",
        description="Change the online vCPUs, cores and scheduler weight of a "
                    "running qube, until it next starts.",
    )
    parser.add_argument("qube")
    parser.add_argument("--backend", choices=KINDS, default="auto",
                        help="where vCPUs come from (default auto: xl when "
                             "installed, demo data otherwise)")
    parser.add_argument("--mock", dest="backend", action="store_const", const="mock",
                        help="same as --backend mock")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"the daemon's cpu-affinity.json (default {DEFAULT_CONFIG})")
    parser.add_argument("-a", "--alpha", type=float, default=WINDOW_ALPHA,
                        help=f"window background opacity, 0.0-1.0 (default {WINDOW_ALPHA})")
    args = parser.parse_args(argv)
    args.alpha = max(0.0, min(1.0, args.alpha))
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point. Exit code 6 means there is no display."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO)
    if not Gtk.init_check()[0]:
        return 6
    backend = create_backend(args.backend, args.config)
    logging.getLogger("qubes-vcpu").info("Backend: %s", backend.name)
    VcpuWindow(backend, args.qube, config_path=args.config, alpha=args.alpha,
               on_close=lambda _applied: Gtk.main_quit())
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

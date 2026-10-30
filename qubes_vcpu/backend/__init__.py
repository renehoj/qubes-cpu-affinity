"""Backends providing access to a qube's vCPUs."""

import os

from .base import Backend, BackendError  # noqa: F401

KINDS = ("auto", "xl", "mock")


def create_backend(kind: str = "auto", config_path: str | None = None) -> Backend:
    """Create a backend: ``xl``, ``mock`` or ``auto``.

    ``auto`` uses xl when it is installed (dom0) and demo data otherwise, so
    the window can be developed on any Linux box.
    """
    if kind == "auto":
        # pylint: disable=import-outside-toplevel
        from .xl_backend import XL
        kind = "xl" if os.path.exists(XL) else "mock"
    if kind == "xl":
        from .xl_backend import XlBackend  # pylint: disable=import-outside-toplevel
        return XlBackend(config_path)
    if kind == "mock":
        from .mock import MockBackend  # pylint: disable=import-outside-toplevel
        return MockBackend()
    raise ValueError(f"Unknown backend: {kind}")

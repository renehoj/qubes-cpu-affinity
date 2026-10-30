#!/usr/bin/env python3
"""Packaging for qubes-vcpu (the daemon is installed by hand, see README)."""

import setuptools

setuptools.setup(
    name="qubes_vcpu",
    version="0.1.0",
    description="vCPU window for one qube, in qubes-systemlist's look",
    packages=["qubes_vcpu", "qubes_vcpu.backend"],
    python_requires=">=3.9",
    # Without these the window raises FileNotFoundError when it opens, only on
    # a real install.
    package_data={"qubes_vcpu": ["vcpu.css", "fallback-theme.css"]},
    entry_points={
        "gui_scripts": [
            "qubes-vcpu = qubes_vcpu.frontend:main",
        ],
    },
)

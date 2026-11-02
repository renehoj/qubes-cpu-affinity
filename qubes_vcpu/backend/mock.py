"""In-memory backend with demo data, for development outside dom0.

A 32-CPU machine (an i9-13900K with SMT) and the README's example
cpu-affinity.json, with qubes in the shapes worth trying the window on.
"""

from __future__ import annotations

import threading
import time

from .. import model
from .base import Backend, BackendError

NR_CPUS = 32

CONFIG = {
    "hard_cores": "4-31",
    "tags": {"performance": "4-15", "efficient": "16-31"},
    "default_cores": "4-31",
}

#: name: (class, running, max vcpus, online, soft, weight, has a -dm stubdomain)
_QUBES = {
    "dom0": (model.ADMIN_VM, True, 4, 4, "0-3", 256, False),
    "work": ("AppVM", True, 8, 4, "4-15", 256, False),
    "games": ("StandaloneVM", True, 12, 12, "4-15", 512, True),
    "office": ("AppVM", True, 4, 2, "4-31", 256, False),
    "sys-net": ("AppVM", True, 2, 2, "16-31", 256, True),
    "sys-firewall": ("DispVM", True, 2, 1, "all", 256, False),
    "personal": ("AppVM", True, 1, 1, "4-7", 128, False),
    "vault": ("AppVM", False, 2, 0, "", 256, False),
}

#: name: (tags, vcpus-online feature); what Save changes.
_SAVED = {
    "work": ({"performance"}, "4"),
    "games": ({"performance", "gaming"}, None),
    "office": (set(), "2"),
    "sys-net": ({"efficient"}, None),
    "sys-firewall": (set(), None),
    "personal": (set(), None),
    "vault": (set(), None),
}


class MockBackend(Backend):
    """Simulates a Qubes system. ``delay`` scales the simulated xl time."""

    name = "demo data"
    is_demo = True

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self._lock = threading.RLock()
        self._qubes = {name: list(values) for name, values in _QUBES.items()}
        self.tags = {name: set(tags) for name, (tags, _) in _SAVED.items()}
        self.features = {name: {model.VCPUS_FEATURE: value}
                         for name, (_, value) in _SAVED.items()
                         if value is not None}
        #: Every xl command run (tests).
        self.ran: list[list[str]] = []
        #: (qube, script) for every script run as root in a qube (tests).
        self.ran_as_root: list[tuple[str, str]] = []
        #: Qubes where running as root fails, like a qube without qrexec.
        self.no_qrexec: set[str] = set()

    def _sleep(self, seconds: float) -> None:
        if self.delay:
            time.sleep(seconds * self.delay)

    def get_state(self, name: str) -> model.QubeVcpus:
        self._sleep(0.2)
        with self._lock:
            try:
                klass, running, max_vcpus, online, soft, weight, dm = self._qubes[name]
            except KeyError:
                raise BackendError(f"No such qube: {name}") from None
            if not running:
                return model.QubeVcpus(name, klass, False)
            return model.QubeVcpus(
                name, klass, True, max_vcpus=max_vcpus, online=online,
                softs=(soft,) * max_vcpus, weight=weight, nr_cpus=NR_CPUS,
                stubdomain=f"{name}-dm" if dm else None,
                placements=model.placements_from_config(CONFIG),
                tags=frozenset(self.tags[name]) if name in self.tags else None,
                feature_value=self.features.get(name, {}).get(model.VCPUS_FEATURE))

    def _save(self, name: str, change: model.SaveChange) -> None:
        self._sleep(0.1)
        with self._lock:
            if change.kind == "tag":
                (self.tags[name].add if change.value else self.tags[name].discard)(
                    change.name)
            elif change.value is None:
                self.features.get(name, {}).pop(change.name, None)
            else:
                self.features.setdefault(name, {})[change.name] = change.value

    def _run_as_root(self, name: str, script: str) -> None:
        self._sleep(0.3)
        with self._lock:
            if name in self.no_qrexec:
                raise BackendError("qvm-run exited with 1.")
            self.ran_as_root.append((name, script))

    def _xl(self, args: list[str]) -> str:
        self._sleep(0.1)
        with self._lock:
            self.ran.append(list(args))
            command, domain = args[0], (args[2] if args[0].startswith("sched-") else args[1])
            if domain.endswith("-dm"):
                return ""
            qube = self._qubes.get(domain)
            if qube is None:
                raise BackendError(f"xl {' '.join(args)} failed: no such domain")
            if command == "vcpu-set":
                qube[3] = int(args[2])
            elif command == "vcpu-pin":
                qube[4] = args[4]
            elif command.startswith("sched-"):
                qube[5] = int(args[4])
            return ""

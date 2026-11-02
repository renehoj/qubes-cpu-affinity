"""Backend interface shared by the xl and demo implementations.

Same split as qubes-firewall: the window only talks to a :class:`Backend`, and
everything crossing that boundary is plain data (:class:`QubeVcpus`,
:class:`Settings`).
"""

from __future__ import annotations

import abc

from ..model import (ONLINE_RULE_SCRIPT, QubeVcpus, SaveChange, Settings, adds_vcpus,
                     check, commands, save_changes)


class BackendError(Exception):
    """An operation failed; the message is meant to be shown to the user."""


class Backend(abc.ABC):
    """Reading and changing one qube's vCPUs.

    All methods may block, so the window calls them from worker threads.
    Implementations must be thread-safe.
    """

    #: Shown in the window when it is not the real system.
    name = "backend"
    #: True when nothing this backend does touches a real system.
    is_demo = False

    @abc.abstractmethod
    def get_state(self, name: str) -> QubeVcpus:
        """The vCPUs of ``name`` as Xen has them now."""

    @abc.abstractmethod
    def _xl(self, args: list[str]) -> str:
        """Run ``xl`` with ``args``; its output. BackendError when it fails."""

    @abc.abstractmethod
    def _run_as_root(self, name: str, script: str) -> None:
        """Run the shell ``script`` as root in the qube ``name``.
        BackendError when it fails."""

    @abc.abstractmethod
    def _save(self, name: str, change: SaveChange) -> None:
        """Write ``change`` to the qube ``name`` in qubesd. BackendError when
        it fails."""

    def apply(self, state: QubeVcpus, settings: Settings) -> list[list[str]]:
        """Change the running qube from ``state`` to ``settings``; the xl
        commands that ran. Nothing is written to qubesd.

        Adding vCPUs first makes sure the qube has the udev rule that brings
        them online; without it they would stay offline in the qube.
        """
        problems = check(state, settings)
        if not state.editable:
            problems.insert(0, f"{state.name} cannot be changed here.")
        if problems:
            raise BackendError(" ".join(problems))
        if adds_vcpus(state, settings):
            try:
                self._run_as_root(state.name, ONLINE_RULE_SCRIPT)
            except BackendError as exc:
                raise BackendError(
                    f"Could not add the udev rule that brings new vCPUs online "
                    f"in {state.name}: {exc} Nothing was applied.") from exc
        ran = []
        for args in commands(state, settings):
            try:
                self._xl(args)
            except BackendError as exc:
                done = "; ".join(" ".join(a) for a in ran) or "nothing"
                raise BackendError(f"{exc} (already applied: {done})") from exc
            ran.append(args)
        return ran

    def save(self, state: QubeVcpus,
             settings: Settings) -> tuple[list[list[str]], list[SaveChange]]:
        """Apply ``settings`` to the running qube, then write the tags and
        feature that give it the same at its next start; the xl commands and
        the changes that ran."""
        if not state.savable:
            raise BackendError(f"The tags and features of {state.name} cannot "
                               "be changed here.")
        ran = self.apply(state, settings)
        saved = []
        for change in save_changes(state, settings):
            try:
                self._save(state.name, change)
            except BackendError as exc:
                done = "; ".join(c.text(state.name) for c in saved) or "nothing"
                raise BackendError(f"{exc} (applied to the running qube; already "
                                   f"saved: {done})") from exc
            saved.append(change)
        return ran, saved

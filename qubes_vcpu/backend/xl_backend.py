"""Backend running ``xl`` in dom0.

xl needs root; as any other user it runs through ``sudo -n`` (dom0's user has
password-less sudo). qubesadmin, when installed, says whether the qube exists,
its class and whether it runs, and holds the tags and features Save changes;
xl alone can only tell a running domain, and Save is then not offered.

The udev rule for added vCPUs is written in the qube with ``qvm-run -u root``,
which dom0 may do without sudo. Only its exit code is used.
"""

from __future__ import annotations

import os
import subprocess

from .. import model
from .base import Backend, BackendError

XL = "/usr/sbin/xl"
QVM_RUN = "qvm-run"


class XlBackend(Backend):
    """The real thing."""

    name = "xl"

    def __init__(self, config_path: str | None = None, app=None):
        self.config_path = config_path
        if app is None:
            try:
                import qubesadmin  # pylint: disable=import-outside-toplevel
                app = qubesadmin.Qubes()
            except ImportError:
                app = None
        self.app = app

    def _xl(self, args: list[str]) -> str:
        cmd = [XL] + args
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        try:
            done = subprocess.run(cmd, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise BackendError(f"Could not run {cmd[0]}: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or "").strip() or f"exit {exc.returncode}"
            raise BackendError(f"xl {' '.join(args)} failed: {message}") from exc
        return done.stdout

    def _run_as_root(self, name: str, script: str) -> None:
        # --pass-io: without it qvm-run does not return the script's exit code.
        cmd = [QVM_RUN, "--pass-io", "--no-gui", "--no-autostart", "-u", "root",
               name, script]
        # The qube's output is not trusted, so it is not even read: kept, it
        # would let the qube fill dom0's memory until the timeout.
        try:
            subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60)
        except FileNotFoundError as exc:
            raise BackendError(f"Could not run {QVM_RUN}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BackendError("qvm-run timed out.") from exc
        except subprocess.CalledProcessError as exc:
            raise BackendError(f"qvm-run exited with {exc.returncode}.") from exc

    def _vm(self, name: str):
        try:
            return self.app.domains[name]
        except KeyError:
            raise BackendError(f"No such qube: {name}") from None

    def _qube(self, name: str) -> tuple[str, bool | None]:
        """(class, running) from qubesd; running None without qubesadmin."""
        if name == "dom0":
            return model.ADMIN_VM, True
        if self.app is None:
            return "qube", None
        vm = self._vm(name)
        try:
            return vm.klass, vm.is_running()
        except Exception as exc:  # pylint: disable=broad-except
            raise BackendError(f"Could not read {name}: {exc}") from exc

    def _saved(self, name: str, feature: str) -> tuple[frozenset[str] | None, str | None]:
        """(tags, feature value) from qubesd; tags None when unknown."""
        if name == "dom0" or self.app is None:
            return None, None
        vm = self._vm(name)
        try:
            return frozenset(vm.tags), vm.features.get(feature, None)
        except Exception as exc:  # pylint: disable=broad-except
            raise BackendError(f"Could not read {name}: {exc}") from exc

    def _save(self, name: str, change: model.SaveChange) -> None:
        if self.app is None:
            raise BackendError("qubesadmin is not installed.")
        vm = self._vm(name)
        try:
            if change.kind == "tag" and change.value:
                vm.tags.add(change.name)
            elif change.kind == "tag":
                vm.tags.remove(change.name)
            elif change.value is None:
                del vm.features[change.name]
            else:
                vm.features[change.name] = change.value
        except KeyError:
            pass  # the tag or feature was gone already
        except Exception as exc:  # pylint: disable=broad-except
            raise BackendError(f"{change.text(name)} failed: {exc}") from exc

    def _exists(self, domain: str) -> bool:
        try:
            self._xl(["domid", domain])
        except BackendError:
            return False
        return True

    def get_state(self, name: str) -> model.QubeVcpus:
        klass, running = self._qube(name)
        if running is False:
            return model.QubeVcpus(name, klass, False)
        xl_name = model.DOM0_XL_NAME if klass == model.ADMIN_VM else name
        if running is None and not self._exists(xl_name):
            raise BackendError(f"{name} is not running, or is no qube.")
        try:
            config = model.load_config(self.config_path)
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        feature = config.get("vcpus_feature") or model.VCPUS_FEATURE
        tags, feature_value = self._saved(name, feature)
        try:
            info = model.parse_xl_info(self._xl(["info"]))
            scheduler = info.get("xen_scheduler", "credit")
            nr_cpus = int(info["nr_cpus"])
            online = model.parse_xl_list(self._xl(["list", xl_name]))
            softs = model.parse_vcpu_list(self._xl(["vcpu-list", xl_name]))
            weight = model.parse_sched(
                self._xl([f"sched-{scheduler}", "-d", xl_name]))
        except (KeyError, ValueError) as exc:
            raise BackendError(f"Could not read the vCPUs of {name}: {exc}") from exc
        stubdomain = None
        if klass != model.ADMIN_VM and self._exists(f"{name}-dm"):
            stubdomain = f"{name}-dm"
        return model.QubeVcpus(
            name, klass, True, max_vcpus=len(softs), online=online, softs=softs,
            weight=weight, scheduler=scheduler, nr_cpus=nr_cpus,
            stubdomain=stubdomain,
            placements=model.placements_from_config(config),
            tags=tags, vcpus_feature=feature, feature_value=feature_value)

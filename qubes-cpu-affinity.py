#!/usr/bin/env python3
"""Qubes CPU affinity daemon.

Listens for domain lifecycle events and sets hard + soft CPU affinity for each
VM. Configuration lives in a JSON file so the core ranges and tags can be
changed without editing code.

Intended to run in dom0 as a systemd service (see qubes-cpu-affinity.service).

Affinity model (two masks, per `xl vcpu-pin`):
  * hard affinity -- the restriction: which CPUs a VM's vCPUs MAY run on.
    Applied uniformly to every VM (usually "all cores except dom0's").
  * soft affinity -- the preference: where the scheduler should place them,
    allowing spillover onto other hard-affinity CPUs when busy. Carries the
    P-core vs E-core split.

SOFT AFFINITY REQUIRES THE CREDIT1 SCHEDULER. Qubes defaults to credit2, which
ignores soft affinity -- add `sched=credit` to GRUB_CMDLINE_XEN_DEFAULT or the
soft mask will be silently dropped.

Why the Admin API + events (instead of a boot-time oneshot):
  * start events also fire for VMs started AFTER boot, which a oneshot that
    runs once at boot would miss.
  * the dispatcher reconnects automatically if qubesd restarts.

Why "xl vcpu-pin" (instead of the libvirt xen-user.xml template):
  * xen-user.xml is a coarse per-VM cpuset baked at VM-create time and is NOT
    applied to dom0 or sys-*-dm stubdomains. vcpu-pin is immediate and works on
    every domain, with separate hard/soft masks -- what a placement tool needs.

HVM VMs also get a device-model stubdomain named <vm>-dm (not a qube, so no
event fires for it); the daemon pins it to the same affinity as its parent.

Online vCPU count: a VM with the qvm-feature named by 'vcpus_feature'
(default "vcpus-online") gets `xl vcpu-set <vm> N`, bringing N of its vCPUs
online. N may not exceed the VM's maxvcpus (qvm-prefs vcpus); if it does, a
warning is logged and the VM is left alone.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time

import qubesadmin
import qubesadmin.events

log = logging.getLogger("qubes-cpu-affinity")

# systemd's PATH is minimal -- always use the absolute path to xl.
XL = "/usr/sbin/xl"

# Default config lives beside this script (~/.local/bin/cpu-affinity.json).
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cpu-affinity.json"
)

DEFAULTS = {
    "tags": {},               # {tag: soft cores}; first matching tag (insertion order) wins
    "vcpus_feature": "vcpus-online",  # qvm-feature holding the online vCPU count
    "retry_attempts": 6,      # the domain may not be in Xen yet when the event fires
    "retry_delay": 0.5,       # seconds between retries
}
# hard_cores / default_cores are deliberately REQUIRED in the config file:
# silently defaulting to the wrong core range would be worse than failing loud.


def load_config(path):
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        log.error("config file not found: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("config file is not valid JSON (%s): %s", path, e)
        sys.exit(1)
    missing = [k for k in ("hard_cores", "default_cores") if k not in cfg]
    if missing:
        log.error("config %s is missing required key(s): %s",
                  path, ", ".join(missing))
        sys.exit(1)
    return cfg


def run_xl(args, what, attempts, delay):
    """Run an xl command, retrying for domain readiness. `what` describes the
    action for log messages. Returns True on success, False if it gave up."""
    cmd = [XL] + args
    for i in range(1, attempts + 1):
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return True
        except FileNotFoundError:
            log.error("could not find xl at %s", XL)
            return False
        except subprocess.CalledProcessError as e:
            log.warning("%s failed (%d/%d): %s",
                        what, i, attempts, e.stderr.strip())
            time.sleep(delay)
    log.error("gave up: %s", what)
    return False


def pin_vm(name, hard, soft, attempts, delay):
    """Set hard + soft affinity for all vCPUs of a domain."""
    return run_xl(["vcpu-pin", name, "all", hard, soft],
                  "pin %s hard=%s soft=%s" % (name, hard, soft),
                  attempts, delay)


def vcpus_for(vm, cfg):
    """Return the online vCPU count requested via the VM's qvm-feature, or
    None if unset or unusable (invalid / above the VM's maxvcpus)."""
    feature = cfg["vcpus_feature"]
    value = vm.features.get(feature, None)
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except ValueError:
        log.warning("%s: feature %s=%r is not an integer, ignoring",
                    vm.name, feature, value)
        return None
    if n < 1:
        log.warning("%s: feature %s=%d must be >= 1, ignoring",
                    vm.name, feature, n)
        return None
    maxvcpus = vm.vcpus
    if n > maxvcpus:
        log.warning("%s: feature %s=%d exceeds maxvcpus %d "
                    "(qvm-prefs vcpus), ignoring", vm.name, feature, n, maxvcpus)
        return None
    return n


def configure_vm(vm, cfg, prefix="", suffix=""):
    """Set the online vCPU count (VM only, not its -dm stubdomain) and hard +
    soft affinity (VM and stubdomain). prefix/suffix decorate log lines."""
    n = vcpus_for(vm, cfg)
    if n is not None and run_xl(["vcpu-set", vm.name, str(n)],
                                "vcpu-set %s %d" % (vm.name, n),
                                cfg["retry_attempts"], cfg["retry_delay"]):
        log.info("%sset %s vcpus -> %d%s", prefix, vm.name, n, suffix)
    hard, soft = affinity_for(vm, cfg)
    for name in domains_for(vm):
        if pin_vm(name, hard, soft, cfg["retry_attempts"], cfg["retry_delay"]):
            log.info("%spinned %s -> hard=%s soft=%s%s",
                     prefix, name, hard, soft, suffix)


def affinity_for(vm, cfg):
    """Return (hard, soft) affinity masks for a VM based on its tags.

    The first matching tag in the 'tags' map (insertion order) wins; a VM
    carrying none of them falls back to default_cores.
    """
    for tag, cores in cfg.get("tags", {}).items():
        if tag in vm.tags:
            return cfg["hard_cores"], cores
    return cfg["hard_cores"], cfg["default_cores"]


def domains_for(vm):
    """Xen domain names to pin for a VM: itself, plus the device-model
    stubdomain (<name>-dm) that HVM VMs get. PV/PVH VMs have none."""
    names = [vm.name]
    if vm.virt_mode == "hvm":
        names.append(vm.name + "-dm")
    return names


def initial_sweep(app, cfg):
    """Pin VMs already running at startup (their start event pre-dated us)."""
    for vm in app.domains:
        if vm.name == "dom0" or not vm.is_running():
            continue
        configure_vm(vm, cfg, prefix="sweep: ")


def make_handler(app, cfg):
    def handler(subject, event, **kwargs):
        try:
            vm = app.domains[str(subject)]
        except KeyError:
            # e.g. a stubdomain or a domain outside the admin view
            log.info("ignoring %s for unknown domain %s", event, subject)
            return
        configure_vm(vm, cfg, suffix=" (event %s)" % event)
    return handler


def main():
    p = argparse.ArgumentParser(description="Qubes CPU affinity daemon")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p.add_argument("--event", default="domain-start",
                   help="event to pin on (domain-start or domain-spawn)")
    p.add_argument("--no-sweep", action="store_true",
                   help="skip the startup sweep of already-running VMs")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    app = qubesadmin.Qubes()

    if not args.no_sweep:
        initial_sweep(app, cfg)

    dispatcher = qubesadmin.events.EventsDispatcher(app)
    dispatcher.add_handler(args.event, make_handler(app, cfg))
    log.info("listening for %s events (hard=%s, tags=%r, default_soft=%s, "
             "vcpus_feature=%s)", args.event, cfg["hard_cores"],
             cfg.get("tags", {}), cfg["default_cores"], cfg["vcpus_feature"])
    asyncio.run(dispatcher.listen_for_events())


if __name__ == "__main__":
    main()

"""What the vCPU window shows and changes, as plain data.

A qube's vCPU state is read from Xen (``xl``): Apply changes the *running*
domain only, and the next start gets the qube's usual settings again (the
cpu-affinity daemon pins it as always). Save applies too, and also writes what
the daemon reads at the next start -- the qube's core tag and its vcpus feature
(:func:`save_changes`). The weight is not saved: the daemon does not set it.

Three things can be changed, each for every vCPU of the qube at once:

* how many vCPUs are online (``xl vcpu-set``), from 1 to the qube's maxvcpus.
  Before vCPUs are added, the qube gets a udev rule that brings them online
  (:data:`ONLINE_RULE`), unless it has one;
* the soft affinity (``xl vcpu-pin <qube> all - <cores>``): one of the core
  sets in cpu-affinity.json -- the tags (P-cores, E-cores) or default_cores.
  The hard affinity is left alone. HVM qubes' ``<qube>-dm`` stubdomain gets
  the same soft affinity, as the daemon does it;
* the scheduler weight (``xl sched-credit -d <qube> -w N``), default 256.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

ADMIN_VM = "AdminVM"
#: Xen's name for dom0.
DOM0_XL_NAME = "Domain-0"

DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".local", "bin",
                              "cpu-affinity.json")

#: The daemon's default for its ``vcpus_feature`` setting.
VCPUS_FEATURE = "vcpus-online"

WEIGHT_MIN = 1
WEIGHT_MAX = 65535
WEIGHT_DEFAULT = 256

#: Placement key of the config's default_cores.
DEFAULT = "default"
#: Placement key of a soft affinity that matches none of the config's sets.
CURRENT = "current"

TAG_LABELS = {
    "performance": "P-cores",
    "efficient": "E-cores",
}

#: Brings a hot-added vCPU online in the qube. Xen's driver only makes it
#: present; Fedora ships this rule (systemd's 40-redhat.rules), Debian does not.
ONLINE_RULE = ('SUBSYSTEM=="cpu", ACTION=="add", TEST=="online", '
               'ATTR{online}=="0", ATTR{online}="1"')
ONLINE_RULE_FILE = "/etc/udev/rules.d/99-qubes-vcpu-online.rules"
#: Run as root in the qube before vCPUs are added: writes the rule when no
#: rules file has it yet. In an AppVM /etc is reset at the next start, which is
#: fine -- the next increase writes it again.
ONLINE_RULE_SCRIPT = (
    f"grep -qsF '{ONLINE_RULE}' /etc/udev/rules.d/*.rules "
    "/usr/lib/udev/rules.d/*.rules /lib/udev/rules.d/*.rules || "
    f"{{ echo '{ONLINE_RULE}' > {ONLINE_RULE_FILE} && udevadm control --reload; }}"
)

ADMIN_NOTE = ("dom0 is pinned at boot (dom0_max_vcpus, dom0_vcpus_pin); "
              "this window only shows it.")
LIVE_NOTE = ("Changes apply to the running qube only. It starts with its usual "
             "settings next time.")
SAVE_NOTE = ("Apply changes the running qube only. Save also sets its core tag "
             "and vCPU feature, so it starts with the same vCPUs and cores next "
             "time (the weight is not saved).")


# -- core sets -----------------------------------------------------------------
def parse_cpus(spec: str, nr_cpus: int) -> frozenset[int]:
    """The CPUs of a Xen ``cpus`` string: ``all``, ``none``, ``4-15``,
    ``0,2,4``, ``^3`` (exclude). Excludes alone start from all CPUs."""
    spec = spec.strip()
    if spec == "all":
        return frozenset(range(nr_cpus))
    if spec in ("none", ""):
        return frozenset()
    include: set[int] = set()
    exclude: set[int] = set()
    for item in spec.split(","):
        item = item.strip()
        target = include
        if item.startswith("^"):
            target, item = exclude, item[1:]
        if item == "all":
            target.update(range(nr_cpus))
            continue
        first, _, last = item.partition("-")
        try:
            low = int(first)
            high = int(last) if last else low
        except ValueError:
            raise ValueError(f"Not a CPU list: {spec!r}") from None
        if low > high:
            raise ValueError(f"Not a CPU list: {spec!r}")
        target.update(range(low, high + 1))
    if not include and exclude:
        include = set(range(nr_cpus))
    return frozenset(include - exclude)


def format_cpus(cpus: frozenset[int]) -> str:
    """``4-15,20`` for a CPU set; ``none`` for an empty one."""
    if not cpus:
        return "none"
    ordered = sorted(cpus)
    ranges: list[str] = []
    start = prev = ordered[0]
    for cpu in ordered[1:] + [None]:
        if cpu is not None and cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        if cpu is not None:
            start = prev = cpu
    return ",".join(ranges)


@dataclass(frozen=True)
class Placement:
    """A soft affinity choice: ``cores`` is the Xen string written to vcpu-pin."""
    key: str
    label: str
    cores: str

    def text(self) -> str:
        return f"{self.label} ({self.cores})"


def load_config(path: str | None = None) -> dict:
    """cpu-affinity.json as a dict. Empty when the file is missing; ValueError
    when unreadable."""
    path = path or DEFAULT_CONFIG
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read {path}: {exc}") from exc


def load_placements(path: str | None = None) -> tuple[Placement, ...]:
    """The core sets in cpu-affinity.json: its tags, in order, then
    default_cores. Empty when the file is missing; ValueError when unreadable."""
    return placements_from_config(load_config(path))


def placements_from_config(config: dict) -> tuple[Placement, ...]:
    placements = [Placement(tag, TAG_LABELS.get(tag, tag), str(cores))
                  for tag, cores in config.get("tags", {}).items()]
    if "default_cores" in config:
        placements.append(Placement(DEFAULT, "No preference",
                                    str(config["default_cores"])))
    return tuple(placements)


# -- state ---------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """What the window changes. ``placement`` is a :class:`Placement` key."""
    online: int
    placement: str
    weight: int


@dataclass(frozen=True)
class QubeVcpus:
    """One qube's vCPUs as Xen has them now.

    ``softs`` holds each vCPU's soft affinity as xl prints it. A qube that is
    not running has only ``name``, ``klass`` and ``running``. ``tags`` and
    ``feature_value`` are what qubesd has for Save; ``tags`` is None when they
    could not be read, and Save is then not offered.
    """
    name: str
    klass: str
    running: bool
    max_vcpus: int = 0
    online: int = 0
    softs: tuple[str, ...] = ()
    weight: int = WEIGHT_DEFAULT
    scheduler: str = "credit"
    nr_cpus: int = 0
    #: The HVM device-model stubdomain, ``<qube>-dm``, when it exists.
    stubdomain: str | None = None
    placements: tuple[Placement, ...] = field(default=())
    tags: frozenset[str] | None = None
    #: The qvm-feature the daemon reads the online vCPUs from, and its value.
    vcpus_feature: str = VCPUS_FEATURE
    feature_value: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.klass == ADMIN_VM

    @property
    def editable(self) -> bool:
        return self.running and not self.is_admin

    @property
    def savable(self) -> bool:
        return self.editable and self.tags is not None

    @property
    def xl_name(self) -> str:
        return DOM0_XL_NAME if self.is_admin else self.name

    @property
    def domains(self) -> tuple[str, ...]:
        """The Xen domains a soft affinity change goes to."""
        if self.stubdomain:
            return (self.xl_name, self.stubdomain)
        return (self.xl_name,)

    def soft_cpus(self) -> frozenset[int] | None:
        """The soft affinity all vCPUs share; None if they differ."""
        sets = {parse_cpus(soft, self.nr_cpus) for soft in self.softs}
        return sets.pop() if len(sets) == 1 else None

    def choices(self) -> tuple[Placement, ...]:
        """The placements to offer: the config's, and the current soft
        affinity first when it is none of them."""
        if self.current_placement() == CURRENT:
            return (self.current_as_placement(),) + self.placements
        return self.placements

    def current_as_placement(self) -> Placement:
        cpus = self.soft_cpus()
        cores = "mixed" if cpus is None else format_cpus(cpus)
        return Placement(CURRENT, "Current", cores)

    def current_placement(self) -> str:
        cpus = self.soft_cpus()
        if cpus is not None:
            for placement in self.placements:
                try:
                    if parse_cpus(placement.cores, self.nr_cpus) == cpus:
                        return placement.key
                except ValueError:
                    continue
        return CURRENT

    def current(self) -> Settings:
        return Settings(self.online, self.current_placement(), self.weight)

    def placement(self, key: str) -> Placement:
        for placement in self.choices():
            if placement.key == key:
                return placement
        raise KeyError(key)


def summary(state: QubeVcpus, settings: Settings | None = None) -> str:
    """The subtitle: class, online vCPUs, weight, scheduler, and whether
    ``settings`` are applied and saved."""
    if not state.running:
        return f"{state.klass} · not running"
    settings = settings or state.current()
    text = (f"{state.klass} · {settings.online} of {state.max_vcpus} vCPUs online"
            f" · weight {settings.weight} · {state.scheduler} scheduler")
    if settings != state.current():
        text += " · not applied"
    if state.savable and save_changes(state, settings):
        text += " · not saved"
    return text


def notes(state: QubeVcpus, config_path: str | None = None) -> list[str]:
    """What the window says above the controls, most important first."""
    if state.is_admin:
        return [ADMIN_NOTE]
    if not state.running:
        return [f"{state.name} is not running. This window only changes a "
                "running qube."]
    found = []
    if state.scheduler != "credit":
        found.append(f"Xen runs the {state.scheduler} scheduler, which ignores "
                     "soft affinity: the core choice has no effect. Boot with "
                     "sched=credit.")
    if not state.placements:
        found.append(f"No core sets in {config_path or DEFAULT_CONFIG}: the "
                     "cores cannot be changed.")
    return found


# -- applying ------------------------------------------------------------------
def check(state: QubeVcpus, settings: Settings) -> list[str]:
    """What is wrong with ``settings``; empty when they can be applied."""
    problems = []
    if not 1 <= settings.online <= state.max_vcpus:
        problems.append(f"Online vCPUs must be 1 to {state.max_vcpus}.")
    if not WEIGHT_MIN <= settings.weight <= WEIGHT_MAX:
        problems.append(f"The weight must be {WEIGHT_MIN} to {WEIGHT_MAX}.")
    try:
        state.placement(settings.placement)
    except KeyError:
        problems.append(f"Unknown core set: {settings.placement}.")
    return problems


def adds_vcpus(state: QubeVcpus, settings: Settings) -> bool:
    """Does ``settings`` bring more vCPUs online? Then the qube needs
    :data:`ONLINE_RULE` first."""
    return settings.online > state.online


def commands(state: QubeVcpus, settings: Settings) -> list[list[str]]:
    """The ``xl`` argument lists that turn the qube's state into ``settings``;
    only what changed."""
    current = state.current()
    found: list[list[str]] = []
    if settings.online != current.online:
        found.append(["vcpu-set", state.xl_name, str(settings.online)])
    if settings.placement != current.placement:
        cores = state.placement(settings.placement).cores
        for domain in state.domains:
            found.append(["vcpu-pin", domain, "all", "-", cores])
    if settings.weight != current.weight:
        found.append([f"sched-{state.scheduler}", "-d", state.xl_name,
                      "-w", str(settings.weight)])
    return found


# -- xl output -----------------------------------------------------------------
def parse_xl_info(text: str) -> dict[str, str]:
    """``xl info`` as ``{key: value}``."""
    info = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip()] = value.strip()
    return info


def _rows(text: str) -> list[list[str]]:
    """The rows of an xl table, without its header line."""
    return [line.split() for line in text.splitlines()[1:] if line.strip()]


def parse_xl_list(text: str) -> int:
    """Online vCPUs from ``xl list <domain>``."""
    rows = _rows(text)
    if not rows or len(rows[0]) < 4:
        raise ValueError(f"Unexpected xl list output: {text!r}")
    return int(rows[0][3])


def parse_vcpu_list(text: str) -> tuple[str, ...]:
    """Each vCPU's soft affinity from ``xl vcpu-list <domain>``; the length
    is the domain's maxvcpus (offline vCPUs are listed too)."""
    softs = []
    for row in _rows(text):
        if "/" not in row:
            raise ValueError(f"Unexpected xl vcpu-list line: {' '.join(row)!r}")
        softs.append(row[row.index("/") + 1])
    return tuple(softs)


def parse_sched(text: str) -> int:
    """The weight from ``xl sched-credit -d <domain>`` (or sched-credit2)."""
    for row in _rows(text):
        if len(row) >= 3 and row[2].isdigit():
            return int(row[2])
    raise ValueError(f"Unexpected xl sched output: {text!r}")


# -- saving --------------------------------------------------------------------
@dataclass(frozen=True)
class SaveChange:
    """One write to qubesd. ``kind`` is ``tag`` (``value``: True adds the tag
    ``name``, False removes it) or ``feature`` (``value``: the new value, None
    unsets it)."""
    kind: str
    name: str
    value: str | bool | None

    def text(self, qube: str) -> str:
        """The same change as a dom0 command, for the log."""
        if self.kind == "tag":
            return f"qvm-tags {qube} {'add' if self.value else 'del'} {self.name}"
        if self.value is None:
            return f"qvm-features --unset {qube} {self.name}"
        return f"qvm-features {qube} {self.name} {self.value}"


def save_changes(state: QubeVcpus, settings: Settings) -> list[SaveChange]:
    """What Save writes so the daemon gives the qube ``settings`` at its next
    start; only what differs from now.

    Cores: the chosen config tag is added and the other config tags removed
    (the daemon takes the first it finds); default_cores removes them all; a
    soft affinity that matches no set (*Current*) leaves the tags alone.
    Online vCPUs: the feature is set, or unset when all vCPUs are online --
    the qube's own vcpus property then decides, as without the daemon.
    """
    found = []
    if settings.placement != CURRENT:
        for placement in state.placements:
            if placement.key == DEFAULT:
                continue
            has = placement.key in (state.tags or ())
            wanted = placement.key == settings.placement
            if has != wanted:
                found.append(SaveChange("tag", placement.key, wanted))
    value = None if settings.online == state.max_vcpus else str(settings.online)
    if value != (state.feature_value or None):
        found.append(SaveChange("feature", state.vcpus_feature, value))
    return found

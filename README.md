# qubes-cpu-affinity

Tag-driven CPU affinity daemon for Qubes OS dom0. Sets **hard + soft** affinity
for each VM based on Qubes tags, reacting to `domain-start` events so VMs
started at any time (not just at boot) get pinned.

## How it works

1. On startup it does a **sweep** over already-running VMs and pins them.
2. It then listens for `domain-start` events and pins each VM as it starts.
3. Two masks are set per VM (see `xl vcpu-pin`):

   - **hard** (`hard_cores`) — the *restriction*. Applied uniformly to every VM;
     typically "all cores except dom0's", so no VM can ever land on dom0.
   - **soft** (`tags` / `default_cores`) — the *preference*. A VM's soft mask is
     the first tag it carries from the `tags` map (`performance` → P-cores,
     `efficient` → E-cores); untagged VMs get the full range (no preference, so
     a single VM can use every core), with spillover allowed.
4. Optionally, the number of **online vCPUs** is set from a qvm-feature (see
   below) via `xl vcpu-set <vm> N`.

**Soft affinity only works with the credit1 scheduler, if you use credit2 
Xen will not respect the limits**

## Install (in dom0)

```bash
# 1. Copy files over (from a qube: qvm-copy-to-dom0, then move into place)
install -D -m 0755 qubes-cpu-affinity.py /home/user/.local/bin/qubes-cpu-affinity.py
install -D -m 0644 cpu-affinity.json /home/user/.local/bin/cpu-affinity.json
sudo install -m 0644 qubes-cpu-affinity.service /etc/systemd/system/

# 2. Edit /home/user/.local/bin/cpu-affinity.json to match your CPU topology.
#    Find P vs E core numbering with:  sudo xenpm get-cpu-topology

# 3. Tag VMs to steer them (untagged = all cores, no preference):
qvm-tags gaming add performance     # prefer P-cores
qvm-tags sys-net add efficient      # prefer E-cores

#    Optionally bring only N vCPUs online (N <= qvm-prefs <vm> vcpus):
qvm-features gaming vcpus-online 2  # -> xl vcpu-set gaming 2

# 4. Enable + start:
sudo systemctl daemon-reload
sudo systemctl enable --now qubes-cpu-affinity

# 5. Verify (Hard / Soft columns):
watch -d -n 1 "xl vcpu-list"
```

## Config

`/home/user/.local/bin/cpu-affinity.json` (next to the script) — `hard_cores` and
`default_cores` are required (the daemon fails loudly if they're missing):

- `hard_cores` — hard affinity for every VM (the restriction; usually `4-31` = everything except dom0).
- `tags` — map of `{tag: soft cores}`. A VM's soft affinity is the first tag it
  carries (top-to-bottom, first match wins). `performance` → P-cores,
  `efficient` → E-cores.
- `default_cores` — soft affinity for VMs carrying none of the tags (all cores, no preference).
- `vcpus_feature` — name of the qvm-feature holding a VM's online vCPU count
  (default `vcpus-online`). Unset/empty = leave the VM's vCPUs alone.
- `retry_attempts` / `retry_delay` — resilience for the start-time race.

Core specs are Xen `cpus` strings: ranges `0-5`, lists `0,2,4`, or `^`-excludes.

Example for an i9-13900K with SMT enabled (32 logical CPUs, P-cores first),
with dom0 pinned to the first 2 P-cores (CPUs 0-3) via the boot param:

```json
{
  "hard_cores": "4-31",
  "tags": {
    "performance": "4-15",
    "efficient": "16-31"
  },
  "default_cores": "4-31"
}
```

Confirm your own split with `sudo xenpm get-cpu-topology` before trusting any
hardcoded range — firmware can reorder cores.

## The vCPU window (`qubes-vcpu`)

<img width="607" height="320" alt="vcpus2" src="https://github.com/user-attachments/assets/7fe3d78f-0269-401e-9d90-3ed2a43e4043" />

You can call it from the active windows using a simple script

```bash
#!/bin/bash

CUR_WIN_ID=`xdotool getwindowfocus`
CUR_VM=`xprop _QUBES_VMNAME -id $CUR_WIN_ID |cut -d \" -f2`

#qubes-vm-settings $CUR_VM
/home/user/.local/share/qubes-cpu-affinity/bin/qubes-vcpu $CUR_VM

exit 0
```

It changes, for every vCPU of the qube at once:

| Control | Range | Runs |
|---|---|---|
| Online vCPUs (slider) | 1 to the qube's maxvcpus (`qvm-prefs vcpus`) | `xl vcpu-set <qube> N` (adding: udev rule first, below) |
| Cores | the `tags` core sets of cpu-affinity.json (P-cores, E-cores) and `default_cores` | `xl vcpu-pin <qube> all - <cores>` (and `<qube>-dm`) |
| Scheduler weight | 1–65535, default 256 | `xl sched-credit -d <qube> -w N` |

**Apply** changes the running qube only: no tag, feature or property is
changed, and the qube's next start gets its usual settings, set by the daemon
as always (a daemon restart re-pins running qubes too). Only the soft affinity
changes; the hard affinity stays `hard_cores`. A soft affinity that matches
none of the config's sets shows as *Current* and is kept unless you pick another.

**Save** applies too, and also writes what the daemon reads at the next start:

| Choice | Saved as |
|---|---|
| Cores: P-cores / E-cores (a `tags` set) | that tag added, the config's other tags removed (`qvm-tags`) |
| Cores: No preference (`default_cores`) | all the config's tags removed |
| Cores: *Current* | tags left alone |
| Online vCPUs below maxvcpus | the `vcpus_feature` set, e.g. `qvm-features <qube> vcpus-online 2` |
| All vCPUs online | the feature unset |

The scheduler weight is not saved: the daemon does not set it. Save needs
qubesadmin (dom0) and is not offered for dom0. The subtitle says *not saved*
while the choice differs from what the qube would get at its next start.

Adding vCPUs needs a udev rule in the qube: Xen's driver only makes a
hot-added vCPU *present*, and udev has to bring it online:

```
SUBSYSTEM=="cpu", ACTION=="add", TEST=="online", ATTR{online}=="0", ATTR{online}="1"
```

Fedora templates ship it (systemd's `40-redhat.rules`), Debian templates do
not. So before raising the count, the window runs `qvm-run -u root` in the
qube: if no rules file in `/etc/udev/rules.d`, `/usr/lib/udev/rules.d` or
`/lib/udev/rules.d` has the rule, it writes
`/etc/udev/rules.d/99-qubes-vcpu-online.rules` and reloads udev. In an
AppVM `/etc` is reset at the next start; the next increase writes it again.
If that fails (no qrexec, a Windows qube), nothing is applied. Lowering the
count needs no rule.

The core sets are read from `~/.local/bin/cpu-affinity.json` (`--config` for
another file). xl runs through `sudo -n` when the window does not run as root.
dom0 and qubes that are not running are shown read-only. Ctrl+Enter applies,
Ctrl+S saves, Escape closes.


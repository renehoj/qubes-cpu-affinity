# qubes-cpu-affinity

CPU placement for [Qubes OS](https://www.qubes-os.org/) on hybrid CPUs
(Intel P-cores + E-cores). Two parts, both running in dom0:

- **`qubes-cpu-affinity.py`**, a daemon that pins every qube to CPU cores
  based on its Qubes tags, as soon as it starts. Optionally sets how many
  vCPUs are online.
- **`qubes-vcpu`**, a small GTK window that changes a *running* qube's vCPUs
  on the fly: online vCPU count, P-cores or E-cores, and scheduler weight.
  Nothing is saved, so the qube gets its usual settings at the next start.

```console
$ qvm-tags games add performance   # the daemon puts games on P-cores
$ qubes-vcpu games                 # ...and this changes it while it runs
```

## Contents

- [Requirements](#requirements)
- [The daemon](#the-daemon)
  - [How it works](#how-it-works)
  - [Install](#install)
  - [Configuration](#configuration)
  - [Tagging qubes](#tagging-qubes)
- [The vCPU window](#the-vcpu-window)
- [Notes and limitations](#notes-and-limitations)
- [Development](#development)

## Requirements

- Qubes OS 4.x. Everything runs in **dom0**.
- Python 3.9+ and `qubesadmin` (both in dom0 already).
- The window needs GTK 3 through PyGObject (in dom0 already).
- **The credit1 Xen scheduler**, if you want the P/E split to do anything
  (see below).

### ⚠️ Switch Xen to the credit1 scheduler

The P-core / E-core preference is a *soft* affinity. Qubes' default scheduler,
credit2, **ignores soft affinity**. Boot Xen with `sched=credit`:

```bash
sudo nano /etc/default/grub
#   add to GRUB_CMDLINE_XEN_DEFAULT:   sched=credit
sudo cp /boot/grub2/grub.cfg /boot/grub2/grub.cfg.bak   # back up first
sudo grub2-mkconfig -o /boot/grub2/grub.cfg
sudo reboot
```

Check it with `xl info | grep xen_scheduler`, which should say `credit`.
Under credit2 the window still works, but it warns you that the core choice
has no effect.

## The daemon

### How it works

1. At startup it **sweeps** the qubes that are already running and pins them.
2. Then it listens for `domain-start` events and pins every qube as it
   starts, including qubes started long after boot.
3. Each qube gets two masks (`xl vcpu-pin <qube> all <hard> <soft>`):
   - **hard** (`hard_cores`) is the *restriction*: the CPUs a qube may run
     on. It is the same for every qube, usually "all cores except dom0's".
   - **soft** is the *preference*: where the scheduler places the qube when it
     can, with spillover to the other hard cores when they're busy. It comes
     from the first tag the qube carries (`performance` for P-cores,
     `efficient` for E-cores). Untagged qubes get `default_cores`, which
     means no preference.
4. HVM qubes also have a device-model stubdomain, `<qube>-dm`. It gets the
   same masks as its qube.
5. Optionally, a qube's `vcpus-online` feature sets how many of its vCPUs are
   online (`xl vcpu-set <qube> N`).

### Install

Copy the repository to dom0 first. For example, from the qube that holds the
checkout (`dev` here):

```bash
# in dom0
qvm-run --pass-io dev 'tar -C ~/src -cf - qubes-cpu-affinity' | tar -C ~ -xf -
cd ~/qubes-cpu-affinity
```

> Moving files into dom0 is a security decision in Qubes. Read what you copy.

Install the daemon and its configuration:

```bash
install -D -m 0755 qubes-cpu-affinity.py ~/.local/bin/qubes-cpu-affinity.py
install -D -m 0644 cpu-affinity.json     ~/.local/bin/cpu-affinity.json
sudo install -m 0644 qubes-cpu-affinity.service /etc/systemd/system/
```

The service file points at `/home/rwh/.local/bin/qubes-cpu-affinity.py`.
**Change `ExecStart` to your own home directory**, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qubes-cpu-affinity
journalctl -fu qubes-cpu-affinity          # what it pins
watch -d -n 1 xl vcpu-list                 # the Hard / Soft columns
```

### Configuration

`cpu-affinity.json` sits next to the script (use `--config` for another
path). `hard_cores` and `default_cores` are required, and the daemon refuses
to start without them.

| Key | Meaning |
|---|---|
| `hard_cores` | Hard affinity for every qube. Usually every CPU except dom0's. |
| `tags` | `{tag: soft cores}`. A qube gets the first tag it carries, top to bottom. |
| `default_cores` | Soft affinity for qubes with none of the tags. |
| `vcpus_feature` | The qvm-feature holding a qube's online vCPU count. Default `vcpus-online`. |
| `retry_attempts`, `retry_delay` | Retries while the domain appears in Xen. Defaults: 6 and 0.5 s. |

Core lists are Xen `cpus` strings: ranges (`4-15`), lists (`0,2,4`) and
excludes (`^3`).

Example for an i9-13900K with SMT (32 logical CPUs, P-cores first), with dom0
on the first two P-cores (CPUs 0–3):

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

Check your own layout with `sudo xenpm get-cpu-topology` before you trust
these ranges. Firmware can number the cores differently.

### Tagging qubes

```bash
qvm-tags games add performance          # prefer P-cores
qvm-tags sys-net add efficient          # prefer E-cores
qvm-features games vcpus-online 2       # bring 2 vCPUs online at start
```

`vcpus-online` must be between 1 and the qube's `qvm-prefs <qube> vcpus`.
Tags and features take effect at the qube's next start, or when the daemon
restarts.

## The vCPU window

`qubes-vcpu <qube>` opens a window for one running qube. It looks like the
other Qubes tools: it uses the Qubes menu's light or dark stylesheet.

| Control | Range | What it runs |
|---|---|---|
| Online vCPUs (slider) | 1 to the qube's maxvcpus | `xl vcpu-set <qube> N` |
| Cores | the `tags` sets of `cpu-affinity.json` and `default_cores` | `xl vcpu-pin <qube> all - <cores>`, also for `<qube>-dm` |
| Scheduler weight | 1–65535, default 256 | `xl sched-credit -d <qube> -w N` |

Each control applies to every vCPU of the qube. The weight is relative: a
qube with weight 512 gets twice the CPU time of one with 256 when CPUs are
contended.

**Nothing is saved.** No tag, feature or preference is changed. When the
qube next starts, the daemon sets it up as usual. A daemon restart also
re-pins running qubes.

Other details:

- Only the soft affinity changes. The hard affinity stays `hard_cores`.
- If a qube's current cores match none of the config's sets, the window shows
  them as *Current* and keeps them unless you pick another set.
- dom0 and qubes that are not running are shown read-only.
- `xl` needs root, so the window runs it with `sudo -n` when it isn't root.
  In dom0 your user has password-less sudo.
- Keys: **Ctrl+Enter** applies, **Escape** closes without applying.

### Adding vCPUs: the udev rule

When a vCPU is added, Xen's driver in the qube only makes it *present*. udev
has to bring it online with this rule:

```
SUBSYSTEM=="cpu", ACTION=="add", TEST=="online", ATTR{online}=="0", ATTR{online}="1"
```

Fedora templates have it (systemd's `40-redhat.rules`), Debian templates do
not. So before it raises the count, the window runs a small script as root in
the qube with `qvm-run -u root`:

- if no rules file in `/etc/udev/rules.d`, `/usr/lib/udev/rules.d` or
  `/lib/udev/rules.d` has the rule, it writes
  `/etc/udev/rules.d/99-qubes-vcpu-online.rules` and reloads udev;
- in an AppVM, `/etc` is reset at the next start, and the next increase writes
  the rule again;
- if it fails (no qrexec, a Windows qube), nothing is applied and the window
  says why.

The window uses only the script's exit code and never shows the qube's
output. Lowering the count needs no rule.

### Running it

Straight from the checkout in dom0:

```bash
~/qubes-cpu-affinity/bin/qubes-vcpu games
```

Or install it, which gives you a `qubes-vcpu` command:

```bash
pip install --user ~/qubes-cpu-affinity
```

| Option | |
|---|---|
| `--config PATH` | `cpu-affinity.json` to read the core sets from. Default `~/.local/bin/cpu-affinity.json`. |
| `--mock` | Demo data instead of xl, to try it out away from dom0. |
| `-a`, `--alpha` | Window background opacity, 0.0–1.0. Default 0.92. |

## Notes and limitations

- **dom0 is not handled.** Pin it with the Xen boot options
  `dom0_max_vcpus=N dom0_vcpus_pin`, and leave its CPUs out of `hard_cores`.
- **Don't oversubscribe.** A qube with more vCPUs than the cores you give it
  shares those cores between its vCPUs. Keep `vcpus` at or below the size of
  its core set.
- **`vcpu-set` can't go above maxvcpus**, which is `qvm-prefs <qube> vcpus`
  when the qube starts. To allow more, raise `vcpus` and restart the qube.
- **"domain not found" warnings at start**: if the retries don't cover it,
  start the daemon with `--event domain-spawn`, which fires when the Xen
  domain is created.

## Development

The window runs anywhere with GTK 3, using demo data:

```bash
bin/qubes-vcpu --mock games
./run-tests.sh          # the window tests need GTK 3 and a display
```

The code is in:

| Path | |
|---|---|
| `qubes-cpu-affinity.py` | the daemon |
| `qubes_vcpu/model.py` | core sets, xl output parsing, which commands a change needs |
| `qubes_vcpu/window.py` | the GTK window |
| `qubes_vcpu/backend/` | `xl_backend.py` (real xl and qvm-run), `mock.py` (demo data) |

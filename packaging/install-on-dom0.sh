#!/bin/bash
# Install or update qubes-cpu-affinity on dom0, end to end.
#
# Same route as qubes-systemlist's packaging/install-on-dom0.sh: dom0 has no
# networking, so a staging qube (over `qvm-run -p`) clones the repo fresh
# from the git server -- public read, no credentials -- stamps the checkout
# with the commit it came from, and streams one tarball back. dom0 validates
# the archive and only then installs:
#
#   ~/.local/share/qubes-cpu-affinity  the checkout (the vCPU window runs
#                                      from its bin/)
#   ~/.local/bin/qubes-vcpu            symlink to that bin/qubes-vcpu
#   ~/.local/bin/qubes-cpu-affinity.py the daemon
#   ~/.local/bin/cpu-affinity.json     the config -- ONLY if there is none yet;
#                                      an existing one is yours and is kept
#   /etc/systemd/system/qubes-cpu-affinity.service  (via sudo, ExecStart
#                                      pointed at the daemon above)
#
# then enables and restarts the daemon (which re-pins every running qube)
# and proves it stayed up. Running it again is an update.
#
# Consequences worth knowing:
#   * the install tracks origin/main -- committed but not PUSHED work does
#     not deploy;
#   * the staging qube needs `git` and must resolve the git server;
#   * BUILD-COMMIT in the installed tree says exactly which commit it is;
#   * a newer cpu-affinity.json in the repo does not replace yours; compare
#     with ~/.local/share/qubes-cpu-affinity/cpu-affinity.json by hand.
#
# Trust note: whatever the staging qube hands over becomes code running in
# dom0 -- the daemon even as root. If that ever stops feeling like enough,
# the place to verify a signature is step 2, before anything is extracted.
#
# Usage: install-on-dom0.sh [staging-qube]      (default: personal)
#
# Environment overrides:
#   QUBES_CPU_AFFINITY_REPO -- clone URL of qubes-cpu-affinity

set -euo pipefail

REPO_URL="${QUBES_CPU_AFFINITY_REPO:-https://git.homelab.computer/homelab/qubes-cpu-affinity.git}"
STAGING_VM="${1:-personal}"        # the qube that talks to the git server
REPO="qubes-cpu-affinity"
TARBALL="$REPO.tgz"                # local name for the streamed archive
TOP_DIR="$REPO-build"              # top-level directory inside the tarball
SHARE_DIR="$HOME/.local/share"
BIN_DIR="$HOME/.local/bin"
SERVICE="qubes-cpu-affinity.service"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# 1. Build on the staging qube and stream the tarball back. Everything
#    between the heredoc markers runs THERE, not here. Its stdout is the
#    tarball and nothing else may write to it; set -e makes a failed clone
#    abort before anything installed is touched.
qvm-run -p "$STAGING_VM" "REPO_URL='$REPO_URL' bash -s" \
    > "$workdir/$TARBALL" <<'EOS'
set -eu
src="$(mktemp -d)"
trap 'rm -rf "$src"' EXIT
mkdir "$src/qubes-cpu-affinity-build"
cd "$src/qubes-cpu-affinity-build"
git clone --quiet --depth 1 "$REPO_URL" qubes-cpu-affinity >&2
git -C qubes-cpu-affinity rev-parse HEAD > qubes-cpu-affinity/BUILD-COMMIT
rm -rf qubes-cpu-affinity/.git
tar czf - -C "$src" qubes-cpu-affinity-build
EOS

# 2. Prove the archive is whole and carries its provenance BEFORE anything
#    installed is touched.
tar tf "$workdir/$TARBALL" >/dev/null
tar xf "$workdir/$TARBALL" -C "$workdir"
build="$workdir/$TOP_DIR/$REPO"
for check in qubes-cpu-affinity.py cpu-affinity.json "$SERVICE" \
             bin/qubes-vcpu qubes_vcpu/window.py; do
    if [ ! -f "$build/$check" ]; then
        echo "install: $check missing from the archive" >&2
        exit 1
    fi
done
if ! grep -Eq '^[0-9a-f]{40}$' "$build/BUILD-COMMIT" 2>/dev/null; then
    echo "install: $REPO carries no valid BUILD-COMMIT" >&2
    exit 1
fi
python3 -m py_compile "$build/qubes-cpu-affinity.py"

# 3. Swap the checkout: copied next to its target and renamed into place.
mkdir -p "$SHARE_DIR" "$BIN_DIR"
rm -rf "$SHARE_DIR/.$REPO.new"
cp -r "$build" "$SHARE_DIR/.$REPO.new"
rm -rf "$SHARE_DIR/$REPO"
mv "$SHARE_DIR/.$REPO.new" "$SHARE_DIR/$REPO"
chmod 755 "$SHARE_DIR/$REPO/bin/qubes-vcpu"
ln -sfn "$SHARE_DIR/$REPO/bin/qubes-vcpu" "$BIN_DIR/qubes-vcpu"

# 4. Daemon, config and unit. The packaged unit names /home/rwh; point it at
#    this user's ~/.local/bin instead.
install -m 0755 "$build/qubes-cpu-affinity.py" "$BIN_DIR/qubes-cpu-affinity.py"
if [ ! -e "$BIN_DIR/cpu-affinity.json" ]; then
    install -m 0644 "$build/cpu-affinity.json" "$BIN_DIR/cpu-affinity.json"
    echo "install: new $BIN_DIR/cpu-affinity.json -- check it against" \
         "'sudo xenpm get-cpu-topology'" >&2
fi
sed "s|/home/rwh/.local/bin/|$BIN_DIR/|g" "$build/$SERVICE" > "$workdir/$SERVICE"
sudo install -m 0644 "$workdir/$SERVICE" "/etc/systemd/system/$SERVICE"

# 5. Restart and prove it stayed up. The unit is Type=simple with
#    Restart=always, so "active" right after the start says little: wait a
#    moment, then require it to be active with no restarts behind it.
sudo systemctl daemon-reload
sudo systemctl enable --quiet "$SERVICE"
sudo systemctl restart "$SERVICE"
sleep 3
if ! sudo systemctl is-active --quiet "$SERVICE" ||
   [ "$(systemctl show -p NRestarts --value "$SERVICE")" != 0 ]; then
    echo "install: $SERVICE did not stay up:" >&2
    sudo journalctl -u "$SERVICE" -n 20 --no-pager >&2
    exit 1
fi

echo "qubes-cpu-affinity $(cut -c1-10 "$SHARE_DIR/$REPO/BUILD-COMMIT")" \
     "installed and active"

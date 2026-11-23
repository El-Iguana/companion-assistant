#!/usr/bin/env bash
# Install a Cantinero resident on this machine (RESIDENT_PLAN.md R4): the machine with the
# speakers and the microphone. The Cantinero server (the model, the bartenders, their
# memory) stays where it is; this machine listens, talks and plays the music.
#
#   resident/install.sh <server>            # e.g. amd-halo.local: build, install, pair
#   resident/install.sh --update            # pull, rebuild, restart
#   resident/install.sh --pair <server>     # pair again (after "Remove" in Settings → Residents)
#   resident/install.sh --pair-music        # pair the music player with Orpheus again
#   resident/install.sh --uninstall [--purge]
#
# What it sets up, per user and with no root:
#   ~/.local/share/cantinero-resident/        resident.json (tokens, 0600), the source checkout
#   image  localhost/cantinero-resident       the same image as the server, run as PANTHEON_ROLE=resident
#   ~/.config/systemd/user/cantinero-resident.service   the service: local STT/TTS, the line to the server
#   ~/.config/systemd/user/cantinero-shell.service      the avatar window, the tray, the music player
#
# Needs: podman, git, python3 with GTK 4, WebKitGTK 6, GStreamer and libsoup 3 bindings
# (Fedora/Bazzite/Mint desktops have them).
set -euo pipefail

REPO="${CANTINERO_REPO:-https://github.com/El-Iguana/companion-assistant.git}"
BRANCH="${CANTINERO_BRANCH:-master}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/cantinero-resident"
SRC="$DATA/src"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
IMAGE="localhost/cantinero-resident"
PORT="${RESIDENT_PORT:-7867}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

run_resident() {   # python -m resident <args>, in the image, on this machine's data
  local tty=(); [[ -t 0 ]] && tty=(-t)
  podman run --rm -i "${tty[@]}" --network host -e PYTHONUNBUFFERED=1 -e RESIDENT_DATA=/resident \
    -v "$DATA:/resident:Z" "$IMAGE" python3 -m resident "$@"
}

check() {
  command -v podman >/dev/null || die "podman is needed"
  command -v git >/dev/null || die "git is needed"
  python3 - <<'EOF' || die "python3 needs the GTK 4, WebKitGTK 6, GStreamer and libsoup 3 bindings (gi)"
import gi
for ns, v in (("Gtk", "4.0"), ("WebKit", "6.0"), ("Gst", "1.0"), ("GstAudio", "1.0"), ("Soup", "3.0")):
    gi.require_version(ns, v)
from gi.repository import Gst
Gst.init(None)
assert Gst.ElementFactory.find("playbin3"), "no playbin3"
EOF
}

fetch() {
  mkdir -p "$DATA"
  chmod 700 "$DATA"
  if [[ -d "$SRC/.git" ]]; then
    git -C "$SRC" fetch -q origin "$BRANCH" && git -C "$SRC" checkout -q "$BRANCH" && git -C "$SRC" reset -q --hard "origin/$BRANCH"
  else
    git clone -q --branch "$BRANCH" "$REPO" "$SRC"
  fi
}

build() {
  say "Building the image (first time: several minutes; it bakes in the speech models)"
  podman build --format docker -t "$IMAGE" -f "$SRC/Containerfile" "$SRC"
}

units() {
  mkdir -p "$UNITS"
  cat > "$UNITS/cantinero-resident.service" <<EOF
[Unit]
Description=Cantinero resident (local voice, the line to the server)
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=-/usr/bin/podman rm -f cantinero-resident
ExecStart=/usr/bin/podman run --rm --name cantinero-resident --network host \\
  -e PANTHEON_ROLE=resident -e RESIDENT_PORT=$PORT -e RESIDENT_DATA=/resident \\
  -v $DATA:/resident:Z $IMAGE
ExecStop=/usr/bin/podman stop -t 5 cantinero-resident
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  cat > "$UNITS/cantinero-shell.service" <<EOF
[Unit]
Description=Cantinero resident shell (the avatar, the tray, the music player)
PartOf=graphical-session.target
After=graphical-session.target cantinero-resident.service
Wants=cantinero-resident.service

[Service]
Environment=RESIDENT_DATA=$DATA
Environment=RESIDENT_PORT=$PORT
ExecStart=/usr/bin/python3 -u $SRC/desktop/pantheon-desktop.py --url http://127.0.0.1:$PORT/desktop
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
  systemctl --user daemon-reload
}

start() {
  systemctl --user enable --now cantinero-resident.service
  systemctl --user enable cantinero-shell.service
  # Started now only in a running graphical session; otherwise at the next login.
  systemctl --user restart cantinero-shell.service 2>/dev/null || true
  # Run as a service even when nobody is logged in through a terminal.
  loginctl enable-linger "$USER" 2>/dev/null || true
}

pair() {
  say "Pairing with Cantinero at $1"
  run_resident pair "$1"
}

pair_music() {
  say "Pairing the music player with Orpheus"
  run_resident pair-orpheus
}

case "${1:-}" in
  --uninstall)
    systemctl --user disable --now cantinero-shell.service cantinero-resident.service 2>/dev/null || true
    rm -f "$UNITS/cantinero-shell.service" "$UNITS/cantinero-resident.service"
    systemctl --user daemon-reload
    podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    if [[ "${2:-}" == "--purge" ]]; then rm -rf "$DATA"; echo "removed, with its pairing"; else
      echo "removed (the pairing is kept in $DATA; --purge removes it; remove it in Settings → Residents too)"; fi
    ;;
  --update)
    check; fetch; build; units
    systemctl --user restart cantinero-resident.service
    systemctl --user restart cantinero-shell.service 2>/dev/null || true
    say "Updated."
    ;;
  --pair)
    [[ -n "${2:-}" ]] || die "which server? --pair amd-halo.local"
    pair "$2"; systemctl --user restart cantinero-resident.service cantinero-shell.service 2>/dev/null || true
    ;;
  --pair-music)
    pair_music; systemctl --user restart cantinero-shell.service 2>/dev/null || true
    ;;
  ""|-h|--help)
    sed -n '2,20p' "$0"; exit 0
    ;;
  *)
    server="$1"
    check; fetch; build
    pair "$server"
    if grep -q '"orpheus_token"' "$DATA/resident.json" 2>/dev/null; then
      echo "(the music player is already paired with Orpheus)"
    else
      pair_music || echo "(the music can be paired later: $0 --pair-music)"
    fi
    units; start
    say "Installed. The avatar and the music player start with your session; the tray icon has both."
    ;;
esac

#!/usr/bin/env bash
# Echo cancellation for hands-free interruption (Phase 5).
#
# Without this, the microphone hears her through the speakers far louder than it
# hears you -- measured on the reference box, +26 dB over the noise floor. No
# level threshold can separate a signal that dominates by that much, which is why
# the earlier "duck the gate by her output level" approach failed. A real
# canceller takes the same path to -39 dB, i.e. below the noise floor.
#
# Creates two PipeWire nodes:
#   aec_source  echo-cancelled capture -- select this as the mic in Settings
#   aec_sink    playback reference     -- audio MUST go here or there is nothing
#                                         to subtract
#
# --persist writes a PipeWire drop-in so this survives reboot; without it the
# canceller is runtime-only and hands-free silently drops back to half-duplex
# after a restart, which is a confusing way to lose a feature.
# On headphones you need none of this -- just tick "Let me interrupt her".
set -euo pipefail

DROPIN="$HOME/.config/pipewire/pipewire.conf.d/10-pantheon-echo-cancel.conf"

usage() {
  cat <<'U'
usage: setup-aec.sh [option]

  (no option)  load the canceller for this session only
  --persist    install a PipeWire drop-in so it loads at every boot  [recommended]
  --unpersist  remove that drop-in
  --default    also route all system audio through the canceller
  --status     show what is loaded and what the defaults are
  --unload     unload the runtime module
U
  exit 0
}
SET_DEFAULT=0
case "${1:-}" in
  --help|-h) usage ;;
  --status)
    echo "drop-in:       $([ -f "$DROPIN" ] && echo "installed (survives reboot)" || echo "not installed -- runtime only")"
    echo "modules:"; pactl list short modules 2>/dev/null | grep echo-cancel || echo "  (none via pactl; a drop-in loads it natively instead)"
    echo "sources:"; pactl list short sources 2>/dev/null | grep -E 'aec|Name' || true
    echo "default sink:   $(pactl get-default-sink 2>/dev/null)"
    echo "default source: $(pactl get-default-source 2>/dev/null)"
    exit 0 ;;
  --unload)
    ids=$(pactl list short modules 2>/dev/null | awk '/echo-cancel/{print $1}')
    [ -z "$ids" ] && { echo "nothing to unload"; exit 0; }
    for m in $ids; do pactl unload-module "$m"; done
    echo "unloaded; defaults revert on their own"; exit 0 ;;
  --persist)
    PERSIST_DEFAULT=1
    mkdir -p "$(dirname "$DROPIN")"
    cat > "$DROPIN" <<'CONF'
# Pantheon: echo-cancelled capture, loaded at PipeWire start.
#
# Without this the microphone hears her through the speakers far louder than it
# hears you -- measured on the reference machine, +26 dB over the noise floor
# versus fully cancelled through this. Hands-free interruption depends on it.
#
# aec_source is the mic to select in the app. aec_sink is the reference: audio
# must be PLAYED there or the canceller has nothing to subtract, which is the
# easy half of this to get wrong.
context.modules = [
  { name = libpipewire-module-echo-cancel
    args = {
      library.name = aec/libspa-aec-webrtc
      capture.props   = { node.name = "effect_input.pantheon-aec",  node.passive = true }
      source.props    = { node.name = "aec_source",
                          node.description = "Echo-Cancelled Mic (Cantinero)" }
      sink.props      = { node.name = "aec_sink",
                          node.description = "Echo-Cancelled Out (Cantinero)" }
      playback.props  = { node.name = "effect_output.pantheon-aec", node.passive = true }
    }
  }
]
CONF
    echo "installed $DROPIN"
    # Drop any runtime copy first, or the restart yields two of everything.
    for m in $(pactl list short modules 2>/dev/null | awk '/echo-cancel/{print $1}'); do
      pactl unload-module "$m" 2>/dev/null || true
    done
    systemctl --user restart pipewire pipewire-pulse wireplumber
    sleep 3
    if pactl list short sources 2>/dev/null | grep -q '\baec_source\b'; then
      # Selecting aec_source alone cancels nothing: the canceller subtracts what
      # it is PLAYING, so playback has to go through aec_sink too. Getting only
      # half of this right is the obvious failure, so --persist does both.
      pactl set-default-sink aec_sink 2>/dev/null && echo "default sink -> aec_sink"
      for i in $(pactl list short sink-inputs 2>/dev/null | awk '{print $1}'); do
        pactl move-sink-input "$i" aec_sink 2>/dev/null || true
      done
      echo "aec_source is up and will return on every boot"
    else
      echo "WARNING: aec_source did not appear -- check: journalctl --user -u pipewire -n 40"
      exit 1
    fi
    exit 0 ;;
  --unpersist)
    rm -f "$DROPIN" && echo "removed $DROPIN"
    systemctl --user restart pipewire pipewire-pulse wireplumber
    echo "restarted; the canceller is gone"
    exit 0 ;;
  --default) SET_DEFAULT=1 ;;
  "") ;;
  *) usage ;;
esac

command -v pactl >/dev/null || { echo "pactl not found (is PipeWire/Pulse running?)"; exit 1; }

# Pick the first real capture source and the current default sink as masters.
SRC=$(pactl list short sources 2>/dev/null | grep -v '\.monitor' | grep -v 'aec_' | awk 'NR==1{print $2}')
SNK=$(pactl get-default-sink 2>/dev/null)
[ -n "$SRC" ] || { echo "no capture device found -- plug a mic in first"; exit 1; }
echo "mic  : $SRC"
echo "speak: $SNK"

if pactl list short modules 2>/dev/null | grep -q echo-cancel; then
  echo "echo-cancel already loaded (use --unload to reset)"
else
  pactl load-module module-echo-cancel \
    source_name=aec_source sink_name=aec_sink \
    source_master="$SRC" sink_master="$SNK" \
    aec_method=webrtc >/dev/null
  echo "loaded aec_source + aec_sink"
fi

if [ "$SET_DEFAULT" = 1 ]; then
  # Routes ALL system audio through the canceller. Simplest way to guarantee the
  # browser plays into the reference, at the cost of processing everything --
  # worth undoing before gaming.
  pactl set-default-source aec_source
  pactl set-default-sink aec_sink
  echo "defaults set to aec_source / aec_sink"
else
  echo
  echo "Now either:"
  echo "  * re-run with --default (routes all audio through the canceller), or"
  echo "  * pick aec_source as the mic in Settings AND send playback to aec_sink"
  echo "    -- selecting the source alone does nothing; the canceller needs the"
  echo "       playback reference to subtract."
fi

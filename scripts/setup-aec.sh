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
# Runtime only: nothing is written to disk, so a reboot undoes it.
# On headphones you need none of this -- just tick "Let me interrupt her".
set -euo pipefail

usage() { echo "usage: $0 [--default] [--status] [--unload]"; exit 0; }
SET_DEFAULT=0
case "${1:-}" in
  --help|-h) usage ;;
  --status)
    echo "modules:"; pactl list short modules 2>/dev/null | grep echo-cancel || echo "  (none loaded)"
    echo "sources:"; pactl list short sources 2>/dev/null | grep -E 'aec|Name' || true
    echo "default sink:   $(pactl get-default-sink 2>/dev/null)"
    echo "default source: $(pactl get-default-source 2>/dev/null)"
    exit 0 ;;
  --unload)
    ids=$(pactl list short modules 2>/dev/null | awk '/echo-cancel/{print $1}')
    [ -z "$ids" ] && { echo "nothing to unload"; exit 0; }
    for m in $ids; do pactl unload-module "$m"; done
    echo "unloaded; defaults revert on their own"; exit 0 ;;
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

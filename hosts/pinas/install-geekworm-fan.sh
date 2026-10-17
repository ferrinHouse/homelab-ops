#!/bin/bash
# Install Geekworm's PWM fan service for the NASPi Gemini 2.5 (X882) on pinas, pinned and checksummed.
#
# Why: the X882's PWM fan only runs while a script feeds it a PWM signal. Before this was installed pinas had
# NO fan control at all, so the fan was not running while two SSDs and a Pi 4 sat in a closed metal case.
# Heat is one suspect for the JMS561 USB bridge resets (see README "NAS / NFS Outage Recovery").
#
# What it installs: only the fan service (x-c1-fan.sh + x-c1-fan.service) from github.com/geekworm-com/xscript,
# pinned to the commit that was reviewed on 2026-10-16 and verified by SHA-256 before anything is copied. It
# deliberately does NOT install xPWR.sh (safe-shutdown / power button daemon): that one drives the board's
# boot/shutdown handshake pins and should be a separate, supervised decision.
#
# What the fan script does (reviewed): reads /sys/class/thermal/thermal_zone0/temp every 5 s and writes a duty
# cycle to /sys/class/pwm/pwmchip0/pwm0. 0% below 25 C, 40% at 25, 45% at 40, 50% at 50, 70% at 60, 80% at 70,
# 100% at 75. It touches nothing else. NOTE it tracks the Pi's CPU temperature, not the SSDs.
#
# Changes it makes to the Pi: apt install bc (the script needs it), add `dtoverlay=pwm-2chan` to
# /boot/firmware/config.txt, and (optionally, --disable-audio) set dtparam=audio=off because the Pi 4's
# analogue audio driver (snd_bcm2835) uses the same PWM hardware. A REBOOT is needed for the overlay.
#
# Usage:  sudo ./install-geekworm-fan.sh [--disable-audio]
# Not deployed by CI; run by hand on pinas. Undo with: sudo systemctl disable --now x-c1-fan; remove the
# dtoverlay line; rm /usr/local/bin/x-c1-fan.sh /lib/systemd/system/x-c1-fan.service

set -euo pipefail

COMMIT=adcfdd7ac58dd222ccd5e9e4680e0e1872262006
BASE="https://raw.githubusercontent.com/geekworm-com/xscript/$COMMIT"
SHA_FAN_SH=a63c2e09602a551ece0f89182f50e8b1e82e4e9d87dcb2ad412d868000917c9d
SHA_FAN_SVC=3bce0e74a6091d9056ed65accdac8248aba0a0ecbfc9db44c2e9c5284acb087c
CONFIG=/boot/firmware/config.txt

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
DISABLE_AUDIO=0; [ "${1:-}" = "--disable-audio" ] && DISABLE_AUDIO=1

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$BASE/x-c1-fan.sh"      -o "$tmp/x-c1-fan.sh"
curl -fsSL "$BASE/x-c1-fan.service" -o "$tmp/x-c1-fan.service"
echo "$SHA_FAN_SH  $tmp/x-c1-fan.sh"           | sha256sum -c -
echo "$SHA_FAN_SVC  $tmp/x-c1-fan.service"     | sha256sum -c -

apt-get install -y bc
install -m 0755 "$tmp/x-c1-fan.sh" /usr/local/bin/x-c1-fan.sh
install -m 0644 "$tmp/x-c1-fan.service" /lib/systemd/system/x-c1-fan.service

cp -n "$CONFIG" "$CONFIG.pre-geekworm-fan"          # keep the first backup only
grep -q '^dtoverlay=pwm-2chan' "$CONFIG" || echo 'dtoverlay=pwm-2chan' >> "$CONFIG"
if [ "$DISABLE_AUDIO" = 1 ]; then sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG"; fi

systemctl daemon-reload
systemctl enable x-c1-fan
echo
echo "Installed. Reboot to load the PWM overlay (use the supervised reboot: stop NFS clients first, see README)."
echo "After the reboot: systemctl status x-c1-fan ; journalctl -u x-c1-fan -n 5   (expect 'Fan speed changed to ...')"
[ "$DISABLE_AUDIO" = 1 ] || echo "If the service fails with a PWM/busy error, re-run with --disable-audio and reboot again."

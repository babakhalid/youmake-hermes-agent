#!/bin/sh
# Youmake desktop: block until the X server socket for display :N exists.
# Usage: wait-for-display.sh N   (N = display number, default 1)
# Gives Xvfb up to 10 seconds; exits non-zero so systemd retries the unit.
n="${1:-1}"
i=0
while [ ! -S "/tmp/.X11-unix/X$n" ]; do
  i=$((i + 1))
  if [ "$i" -ge 100 ]; then
    echo "display :$n did not come up" >&2
    exit 1
  fi
  sleep 0.1
done
exit 0

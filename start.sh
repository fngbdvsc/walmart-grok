#!/bin/sh
echo "container start"
mkdir -p /tmp/.X11-unix
Xvfb :99 -screen 0 1366x768x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
i=0
while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 25 ]; do
  i=$((i + 1))
  sleep 0.2
done
if [ -e /tmp/.X11-unix/X99 ]; then
  echo "display :99 is up"
else
  echo "display :99 did not start"
  cat /tmp/xvfb.log
fi
exec python -u walmart_monitor.py

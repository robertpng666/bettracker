#!/bin/zsh
# Double-click to start the POTD Tracker. Keep this window open while tracking.
cd "$(dirname "$0")"
exec /usr/bin/python3 -W ignore app.py

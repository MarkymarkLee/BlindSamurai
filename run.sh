#!/bin/sh
# Sets everything up on first run, then starts the game. Arguments pass through, e.g.
#   ./run.sh --sound-test
set -e
cd "$(dirname "$0")"

command -v uv >/dev/null || { echo "uv is required: brew install uv"; exit 1; }
command -v swiftc >/dev/null || { echo "Swift is required: xcode-select --install"; exit 1; }

# Always use this project's own .venv, even if your shell points uv somewhere else.
export UV_PROJECT_ENVIRONMENT=.venv

# MediaPipe pose model.
MODEL=models/pose_landmarker_full.task
if [ ! -f "$MODEL" ]; then
  echo "downloading pose model..."
  mkdir -p models
  curl -fL --progress-bar -o "$MODEL" \
    https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task
fi

# AirPods head tracking + voice helper app; rebuilt when its sources change.
BIN=headtracker/HeadTracker.app/Contents/MacOS/HeadTracker
for f in headtracker/main.swift headtracker/voice.swift headtracker/Info.plist; do
  if [ ! -f "$BIN" ] || [ "$f" -nt "$BIN" ]; then headtracker/build.sh; break; fi
done

# uv creates .venv and installs the locked dependencies the first time.
exec uv run python main.py "$@"

"""AirPods Pro head orientation and voice commands, via the HeadTracker helper app (see headtracker/)."""
import json
import math
import os
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import wrap
from .pose import Slash

APP = Path(__file__).resolve().parent.parent / "headtracker" / "HeadTracker.app"
PORT = int(os.environ.get("HEADTRACKER_PORT", "47811"))
# CoreMotion yaw grows when turning left; we want clockwise. Set HEAD_YAW_SIGN=1 if it comes out mirrored.
YAW_SIGN = float(os.environ.get("HEAD_YAW_SIGN", "-1"))

# A head swing is a sword stroke: fast and far enough that ordinary looking around doesn't count.
SWING_WINDOW_S = 0.25
SWING_MIN_SPEED = 250.0   # degrees per second
SWING_MIN_TRAVEL = 35.0   # degrees covered within the window
SWING_COOLDOWN_S = 0.45


class HeadTracker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="head")
        self.yaw = 0.0          # calibrated, clockwise degrees
        self.pitch = 0.0
        self.raw_yaw: float | None = None
        self.offset = 0.0
        self.last_update = 0.0
        self.status = "starting"
        self.voice_status = "starting"
        self.heard = ""          # latest speech transcript, for the HUD
        self.commands: "queue.Queue[str]" = queue.Queue()   # e.g. "start game"
        self.slashes: "queue.Queue[Slash]" = queue.Queue()  # head swings
        self._motion: deque = deque()   # (t, unwrapped yaw, pitch)
        self._last_swing = 0.0
        self._calibrated = False

    @property
    def active(self) -> bool:
        return time.monotonic() - self.last_update < 0.5

    def calibrate(self):
        """Call while the player looks at the camera."""
        if self.raw_yaw is not None:
            self.offset = self.raw_yaw
            self.yaw = 0.0

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", PORT))
        except OSError:
            self.status = self.voice_status = "unavailable: another copy of the game is using it"
            return
        if not APP.exists():
            self.status = self.voice_status = "helper not built: run headtracker/build.sh"
            return
        # Launched through `open` so macOS treats it as its own app with its own motion permission.
        subprocess.run(["open", "-g", "-n", str(APP), "--args", str(PORT), str(os.getpid())], check=False)
        self.status = "waiting for AirPods"
        while True:
            try:
                msg = json.loads(sock.recv(1024))
            except (OSError, ValueError):
                continue
            if "yaw" in msg:
                self.raw_yaw = YAW_SIGN * math.degrees(msg["yaw"])
                if not self._calibrated:
                    # Assume the player starts out looking at the camera; C recalibrates.
                    self.offset, self._calibrated = self.raw_yaw, True
                self.yaw = wrap(self.raw_yaw - self.offset)
                self.pitch = math.degrees(msg["pitch"])
                self.last_update = time.monotonic()
                self.status = "tracking"
                self._detect_swing(self.last_update)
            elif msg.get("event") == "command":
                self.commands.put(msg.get("text", ""))
            elif msg.get("event") == "heard":
                self.heard = msg.get("text", "")
            elif msg.get("event") == "voice":
                self.voice_status = msg.get("status", "")
            elif msg.get("event") == "status" and not msg.get("active"):
                auth = msg.get("auth")
                self.status = ("motion permission denied (System Settings > Privacy > Motion)"
                               if auth == "denied" else "waiting for AirPods")
            elif msg.get("event") == "disconnected":
                self.status = "AirPods disconnected"
            elif msg.get("event") == "error":
                self.status = "error: " + msg.get("message", "")

    def _detect_swing(self, now):
        hist = self._motion
        # Unwrap yaw so a swing through ±180° still measures as one continuous movement.
        yaw = self.yaw if not hist else hist[-1][1] + wrap(self.yaw - hist[-1][1])
        hist.append((now, yaw, self.pitch))
        while hist and now - hist[0][0] > SWING_WINDOW_S:
            hist.popleft()
        if len(hist) < 3 or now - self._last_swing < SWING_COOLDOWN_S:
            return
        t0, yaw0, pitch0 = hist[0]
        dt = now - t0
        dyaw, dpitch = yaw - yaw0, self.pitch - pitch0
        travel = math.hypot(dyaw, dpitch)
        if dt < 0.05 or travel < SWING_MIN_TRAVEL or travel / dt < SWING_MIN_SPEED:
            return
        self._last_swing = now
        hist.clear()
        if abs(dyaw) >= abs(dpitch):
            direction = "right" if dyaw > 0 else "left"
        else:
            direction = "up" if dpitch > 0 else "down"
        self.slashes.put(Slash(now, "head", direction, travel / dt, sweep=(wrap(yaw0), wrap(yaw))))

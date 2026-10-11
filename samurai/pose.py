"""Camera + MediaPipe pose: body facing direction and sword-slash detection."""
import math
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from . import lerp_angle, wrap

MODEL = Path(__file__).resolve().parent.parent / "models" / "pose_landmarker_full.task"

NOSE, L_EYE, R_EYE, MOUTH_L, MOUTH_R = 0, 2, 5, 9, 10
L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST, L_HIP, R_HIP = 11, 12, 15, 16, 23, 24
SKELETON = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24),
            (23, 25), (25, 27), (24, 26), (26, 28), (0, 11), (0, 12)]

# Slash tuning, in shoulder-widths so it works at any distance from the camera.
SLASH_WINDOW_S = 0.18      # look this far back for the stroke
SLASH_MIN_SPEED = 5.0      # shoulder-widths per second
SLASH_MIN_TRAVEL = 1.1     # shoulder-widths covered within the window
SLASH_COOLDOWN_S = 0.45
# Facing a body flip (front <-> back) must persist this many frames before we believe it.
FLIP_FRAMES = 6


@dataclass
class Slash:
    t: float
    hand: str        # "left", "right", "both", or "head" for an AirPods head swing
    direction: str   # as the player sees it in the mirror: "left", "right", "up", "down", or diagonals
    speed: float     # shoulder-widths per second (degrees per second for a head swing)
    sweep: tuple[float, float] | None = None  # head swing: world yaw it swept from -> to


def list_cameras() -> list[str]:
    """Camera names in the order OpenCV's AVFoundation backend numbers them (best effort)."""
    try:
        import AVFoundation as AVF
        types = [AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera, AVF.AVCaptureDeviceTypeExternal]
        if hasattr(AVF, "AVCaptureDeviceTypeContinuityCamera"):
            types.append(AVF.AVCaptureDeviceTypeContinuityCamera)
        session = AVF.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
            types, AVF.AVMediaTypeVideo, AVF.AVCaptureDevicePositionUnspecified)
        devices = sorted(session.devices(), key=lambda d: str(d.uniqueID()))
        return [str(d.localizedName()) for d in devices]
    except Exception:
        return []


def ensure_camera_access(timeout_s: float = 60.0) -> str:
    """Ask macOS for camera permission on the main thread (OpenCV can't from the pose thread)."""
    os.environ["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
    try:
        import AVFoundation as AVF
        from Foundation import NSDate, NSRunLoop
    except ImportError:
        return "unknown"
    names = {0: "not determined", 1: "restricted", 2: "denied", 3: "authorized"}
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    if status == 0:
        answer = []
        AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVF.AVMediaTypeVideo, lambda granted: answer.append(granted))
        end = time.monotonic() + timeout_s
        while not answer and time.monotonic() < end:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    return names.get(status, str(status))


def default_camera() -> int:
    """Prefer the laptop's built-in camera over a connected phone or webcam."""
    names = list_cameras()
    for i, name in enumerate(names):
        if "MacBook" in name or "FaceTime" in name or "Built-in" in name:
            return i
    return 0


class PoseTracker(threading.Thread):
    def __init__(self, camera: int, mirror: bool = True):
        super().__init__(daemon=True, name="pose")
        self.camera = camera
        self.mirror = mirror
        self.lock = threading.Lock()
        self.slashes: "queue.Queue[Slash]" = queue.Queue()
        self.running = True
        self._switch_to: int | None = None

        # Published state (read by the game under self.lock)
        self.frame: np.ndarray | None = None      # RGB, mirrored if self.mirror
        self.landmarks: list | None = None        # normalized image landmarks, mirrored
        self.detected = False
        self.last_seen = 0.0
        self.body_yaw = 0.0                       # smoothed, calibrated
        self.face_vis = 0.0
        self.fps = 0.0
        self.error = ""

        self.yaw_offset = 0.0
        self._raw_yaw: float | None = None
        self._flip_count = 0
        self._wrists = {"left": deque(), "right": deque()}
        self._last_slash = 0.0

    # ---------- control from the game thread ----------
    def calibrate(self):
        """Call while the player faces the camera: that becomes yaw 0."""
        if self._raw_yaw is not None:
            self.yaw_offset = self._raw_yaw
            self.body_yaw = 0.0

    def switch_camera(self, index: int):
        self._switch_to = index

    def stop(self):
        self.running = False

    # ---------- worker ----------
    def _open(self, index):
        cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap

    def run(self):
        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(MODEL)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        landmarker = vision.PoseLandmarker.create_from_options(options)
        cap = self._open(self.camera)
        t0 = time.monotonic()
        last_ts = -1
        frames, fps_t = 0, time.monotonic()
        while self.running:
            if self._switch_to is not None:
                cap.release()
                self.camera, self._switch_to = self._switch_to, None
                cap = self._open(self.camera)
            ok, bgr = cap.read() if cap.isOpened() else (False, None)
            if not ok:
                self.error = f"camera {self.camera} gives no frames (TAB tries the next one)"
                time.sleep(0.5)
                cap.release()
                cap = self._open(self.camera)
                continue
            self.error = ""
            h, w = bgr.shape[:2]
            if w > 960:  # pose quality is fine at this size and it keeps the frame rate up
                bgr = cv2.resize(bgr, (960, int(h * 960 / w)))
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            now = time.monotonic()
            ts = int((now - t0) * 1000)
            if ts <= last_ts:
                ts = last_ts + 1
            last_ts = ts
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
            self._process(result, rgb.shape, now)
            if self.mirror:
                rgb = np.ascontiguousarray(rgb[:, ::-1])
            with self.lock:
                self.frame = rgb
            frames += 1
            if now - fps_t >= 1.0:
                self.fps = frames / (now - fps_t)
                frames, fps_t = 0, now
        cap.release()
        landmarker.close()

    def _process(self, result, shape, now):
        if not result.pose_landmarks:
            with self.lock:
                self.detected = False
                self.landmarks = None
            return
        img = result.pose_landmarks[0]
        world = result.pose_world_landmarks[0]
        h, w = shape[:2]

        face_vis = float(np.mean([img[i].visibility for i in (NOSE, L_EYE, R_EYE, MOUTH_L, MOUTH_R)]))
        yaw = self._facing(world, face_vis)
        if yaw is not None:
            self._raw_yaw = yaw
            target = wrap(yaw - self.yaw_offset)
            self.body_yaw = lerp_angle(self.body_yaw, target, 0.5)

        self._detect_slash(img, w, h, now)

        lms = [((1 - p.x) if self.mirror else p.x, p.y, p.visibility) for p in img]
        with self.lock:
            self.detected = True
            self.last_seen = now
            self.landmarks = lms
            self.face_vis = face_vis

    def _facing(self, world, face_vis) -> float | None:
        """Chest direction from the shoulder line, with the front/back ambiguity resolved by the face."""
        ls, rs, nose = world[L_SHOULDER], world[R_SHOULDER], world[NOSE]
        if min(ls.visibility, rs.visibility) < 0.3:
            return None
        # MediaPipe world space: x = image right, z = away from the camera. Top-down "east"
        # (the player's right when facing the camera) is -x, and "north" (towards the camera) is -z.
        se, sn = -(ls.x - rs.x), -(ls.z - rs.z)      # right shoulder -> left shoulder
        if math.hypot(se, sn) < 1e-4:
            return None
        yaw = math.degrees(math.atan2(sn, -se))      # forward = shoulder line rotated 90° clockwise
        flipped = wrap(yaw + 180)

        if face_vis > 0.6:
            # Face visible: the nose sits in front of the shoulders, which picks the right normal.
            mid_e, mid_n = -(ls.x + rs.x) / 2, -(ls.z + rs.z) / 2
            he, hn = -nose.x - mid_e, -nose.z - mid_n
            fe, fn = math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
            candidate = yaw if fe * he + fn * hn >= 0 else flipped
        else:
            # No face: the back is to the camera.
            candidate = yaw if abs(yaw) > 90 else flipped

        # MediaPipe sometimes swaps left/right for a frame or two; don't let that spin us around.
        if self._raw_yaw is not None and abs(wrap(candidate - self._raw_yaw)) > 120:
            self._flip_count += 1
            if self._flip_count < FLIP_FRAMES:
                other = wrap(candidate + 180)
                return other
        self._flip_count = 0
        return candidate

    def _detect_slash(self, img, w, h, now):
        ls, rs = img[L_SHOULDER], img[R_SHOULDER]
        sw = math.hypot((ls.x - rs.x) * w, (ls.y - rs.y) * h)
        if sw < 10:
            return
        hands = {}
        for name, idx in (("left", L_WRIST), ("right", R_WRIST)):
            p = img[idx]
            hist = self._wrists[name]
            if p.visibility < 0.4:
                hist.clear()
                continue
            # Mirrored x so directions match what the player sees in the preview.
            x = ((1 - p.x) if self.mirror else p.x) * w / sw
            y = p.y * h / sw
            hist.append((now, x, y))
            while hist and now - hist[0][0] > SLASH_WINDOW_S:
                hist.popleft()
            if len(hist) >= 3:
                t_old, x_old, y_old = hist[0]
                dt = now - t_old
                dx, dy = x - x_old, y - y_old
                travel = math.hypot(dx, dy)
                if dt > 0.04 and travel >= SLASH_MIN_TRAVEL and travel / dt >= SLASH_MIN_SPEED:
                    hands[name] = (dx, dy, travel / dt)

        if not hands or now - self._last_slash < SLASH_COOLDOWN_S:
            return
        if len(hands) == 2:
            dx = sum(v[0] for v in hands.values()) / 2
            dy = sum(v[1] for v in hands.values()) / 2
            speed = max(v[2] for v in hands.values())
            hand = "both"
        else:
            hand, (dx, dy, speed) = next(iter(hands.items()))
        self._last_slash = now
        for hist in self._wrists.values():
            hist.clear()
        self.slashes.put(Slash(now, hand, _direction(dx, dy), speed))


def _direction(dx, dy) -> str:
    ang = math.degrees(math.atan2(-dy, dx)) % 360  # 0 = right, 90 = up
    names = ["right", "up-right", "up", "up-left", "left", "down-left", "down", "down-right"]
    return names[int((ang + 22.5) // 45) % 8]

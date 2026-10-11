"""Haptics on four DataFeel dots: left side, front centre, right side, and the sword wrist.

The torso dots say where monsters are. The wrist dot tracks the closest monster: it
vibrates harder and warms up as it closes in (cold = far, hot = near), goes to full strength
when you can strike it, and kicks on every hit you land or take.

Which physical dot (Modbus address) plays which role is set in the in-game dot setup
screen and saved to dots.json.

Every Modbus write over Bluetooth costs tens of milliseconds, so a worker thread owns the
link and only sends what changed. The game just sets target intensities per role.
"""
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from datafeel.device import VibrationMode, VibrationWaveforms, discover_devices

from . import wrap

ROLES = ["left", "center", "right", "wrist"]
# Body angle of each torso role: 0 = sternum, +90 = right side.
ROLE_ANGLES = {"left": -90.0, "center": 0.0, "right": 90.0}
DEFAULT_ASSIGNMENT = {"left": 1, "center": 2, "right": 3, "wrist": 4}
CONFIG = Path(__file__).resolve().parent.parent / "dots.json"
MAX_ADDRESS = 4

BLE_NAMES = [n.strip() for n in os.environ.get("DATAFEEL_BLE", "DataFeel36").split(",") if n.strip()]
VIBRATION_HZ = 170.0
STEP = 0.1  # intensity quantization; finer steps just flood the link
# Library effects come in fixed strengths, so the intensity setting picks the nearest one.
_BUZZ = [(1.0, VibrationWaveforms.STRONG_BUZZ_P100), (0.8, VibrationWaveforms.BUZZ2_P80),
         (0.6, VibrationWaveforms.BUZZ3_P60), (0.4, VibrationWaveforms.BUZZ4_P40),
         (0.2, VibrationWaveforms.BUZZ5_P20)]
_CLICK = [(1.0, VibrationWaveforms.STRONG_CLICK_P100), (0.6, VibrationWaveforms.STRONG_CLICK_P60),
          (0.3, VibrationWaveforms.STRONG_CLICK_P30)]
_TICK = [(1.0, VibrationWaveforms.SHARP_TICK1_P100), (0.8, VibrationWaveforms.SHARP_TICK2_P80),
         (0.6, VibrationWaveforms.SHARP_TICK3_P60)]


# Wrist temperature range for "how far is the closest monster". Clamped to the same skin-safe
# limits dashboard.py uses; the dot regulates against its own skin sensor.
THERMAL_COLD_C, THERMAL_HOT_C = 22.0, 38.0
THERMAL_MIN_C, THERMAL_MAX_C = 18.0, 40.0


def _nearest(table, strength):
    return min(table, key=lambda e: abs(e[0] - strength))[1]


def load_assignment() -> dict[str, int | None]:
    out = dict(DEFAULT_ASSIGNMENT)
    try:
        saved = json.loads(CONFIG.read_text())
        out.update({r: saved[r] for r in ROLES if r in saved})
    except (OSError, ValueError):
        pass
    return out


def save_assignment(assignment: dict[str, int | None]):
    CONFIG.write_text(json.dumps({r: assignment.get(r) for r in ROLES}, indent=2) + "\n")


def direction_weights(rel_angle: float, spread: float = 100.0) -> dict[str, float]:
    """How strongly each torso dot should fire for something at rel_angle (degrees from the sternum).

    Neighbouring dots share a source in between them: 45° lights center and right together.
    Nothing sits on the back, so 180° lights left and right together, which means "behind".
    """
    w = {r: max(0.0, 1.0 - abs(wrap(rel_angle - ang)) / spread) for r, ang in ROLE_ANGLES.items()}
    top = max(w.values())
    return {r: (v / top if top > 0 else 0.0) for r, v in w.items()}


@dataclass
class _DotState:
    sent: float = -1.0
    temp_sent: float | None = None
    mode: VibrationMode | None = None
    pulse_until: float = 0.0


class Haptics(threading.Thread):
    def __init__(self, enabled: bool = True, intensity: float = 1.0):
        super().__init__(daemon=True, name="haptics")
        self.intensity = max(0.0, min(1.0, intensity))   # overall strength, 0..1; adjustable live
        self.assignment = load_assignment()               # role -> address (or None)
        self.enabled = enabled
        self.status = "off" if not enabled else "connecting"
        self.connected: dict[int, object] = {}            # address -> Dot
        self.target = {r: 0.0 for r in ROLES}             # game writes these
        self.wrist_temp: float | None = None              # °C target on the wrist dot, None = off
        self._state: dict[int, _DotState] = {}
        self._events: list[tuple[list[int], list[VibrationWaveforms], float]] = []
        self._lock = threading.Lock()
        self.running = True

    # ---------- game-side API ----------
    def address(self, role: str) -> int | None:
        return self.assignment.get(role)

    def assign(self, role: str, address: int | None):
        """Put a dot on a role. A dot can only play one role, so its old role gets this role's dot."""
        with self._lock:
            previous = self.assignment.get(role)
            if address is not None:
                for r, a in self.assignment.items():
                    if a == address and r != role:
                        self.assignment[r] = previous
            self.assignment[role] = address
        save_assignment(self.assignment)

    def set_targets(self, levels: dict[str, float]):
        with self._lock:
            for r in self.target:
                self.target[r] = max(0.0, min(1.0, levels.get(r, 0.0)))

    def set_wrist_temp(self, celsius: float | None):
        if celsius is not None:
            celsius = max(THERMAL_MIN_C, min(THERMAL_MAX_C, celsius))
        self.wrist_temp = celsius

    def pulse(self, roles, waveforms, hold_s=0.35):
        """Play a library effect on some roles, overriding their continuous level for hold_s."""
        with self._lock:
            addresses = [self.assignment.get(r) for r in roles]
            self._events.append(([a for a in addresses if a], list(waveforms), hold_s))

    def test_address(self, address: int):
        """Buzz one physical dot at full strength so you can find it on your body."""
        with self._lock:
            self._events.append(([address], [VibrationWaveforms.ALERT_750MS_P100], 0.8))

    def hit(self):
        if self.intensity > 0:
            buzz = _nearest(_BUZZ, self.intensity)
            self.pulse(ROLES, [buzz, buzz], 0.6)

    def kill(self):
        if self.intensity > 0:
            click = _nearest(_CLICK, self.intensity)
            self.pulse(["wrist"], [click, VibrationWaveforms.Rest(0.04), click, VibrationWaveforms.Rest(0.04), click], 0.45)

    def slash(self):
        if self.intensity > 0:
            self.pulse(["wrist"], [_nearest(_TICK, self.intensity)], 0.12)

    def stop(self):
        self.running = False

    # ---------- worker ----------
    def run(self):
        if not self.enabled:
            return
        wanted = max([MAX_ADDRESS] + [a for a in self.assignment.values() if a])
        try:
            found = discover_devices(wanted, ble_names=BLE_NAMES)
        except Exception as e:
            self.status = f"error: {e}"
            return
        self._state = {d.id: _DotState() for d in found}
        self.connected = {d.id: d for d in found}
        if not self.connected:
            # A connected El Jefe stops advertising, so another app holding it hides it from us.
            self.status = f"{'/'.join(BLE_NAMES)} not found (close dashboard.py or other apps using it)"
            return
        self.status = f"dots {sorted(self.connected)} connected"
        for d in self.connected.values():
            self._safe(d.set_led_off)
        try:
            while self.running:
                self._tick()
                time.sleep(0.02)
        finally:
            for d in self.connected.values():
                self._safe(d.stop_vibration)
                self._safe(d.disable_all_thermal)
                self._safe(d.set_led_off)
            try:
                from datafeel import ble
                ble.close_all()
            except Exception:
                pass

    def _tick(self):
        now = time.monotonic()
        with self._lock:
            events, self._events = self._events, []
            wrist = self.assignment.get("wrist")
            per_address: dict[int, float] = {}
            for r, level in self.target.items():
                a = self.assignment.get(r)
                if a is not None:
                    per_address[a] = max(per_address.get(a, 0.0), level)
        for addresses, waves, hold in events:
            for a in addresses:
                d = self.connected.get(a)
                if d is None:
                    continue
                st = self._state[a]
                if self._safe(d.play_vibration_sequence, waves):
                    st.mode = VibrationMode.LIBRARY
                    st.pulse_until = now + hold
                    st.sent = -1.0  # force the continuous level to be re-sent afterwards
        # Dots without a role get 0, so reassigning a dot never leaves it buzzing.
        for a, d in self.connected.items():
            st = self._state[a]
            if now < st.pulse_until:
                continue
            level = round(per_address.get(a, 0.0) * self.intensity / STEP) * STEP
            if abs(level - st.sent) < 1e-6:
                continue
            if st.mode != VibrationMode.MANUAL:
                ok = self._safe(d.play_frequency, VIBRATION_HZ, level)
                if ok:
                    st.mode = VibrationMode.MANUAL
            else:
                ok = self._safe(d.registers.set_vibration_intensity, level)
            if ok:
                st.sent = level
        # Temperature on the wrist dot only, in whole degrees (it changes slowly anyway).
        temp = self.wrist_temp
        for a, d in self.connected.items():
            st = self._state[a]
            want = round(temp) if (a == wrist and temp is not None and self.intensity > 0) else None
            if want == st.temp_sent:
                continue
            if want is None:
                ok = self._safe(d.disable_all_thermal)
            else:
                ok = self._safe(d.activate_thermal_temperature_control, float(want))
            if ok:
                st.temp_sent = want

    def _safe(self, fn, *args) -> bool:
        try:
            fn(*args)
            return True
        except Exception:
            return False

"""Head-tracked spatial audio.

Every sound has a position in the world (azimuth + distance). Each audio block we render it
relative to where the head points right now (AirPods yaw), with the cues the ear uses:
level difference between ears, a sub-millisecond delay to the far ear, and a duller,
quieter sound for things behind you. Turning your head therefore moves the whole scene,
music included, the way AirPods spatial audio does.
"""
import math
import random
import threading

import numpy as np
import sounddevice as sd
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import butter, lfilter, sosfilt

from . import sounds, wrap

SR = sounds.SR
BLOCK = 512
MAX_ITD = int(0.00068 * SR)  # ~0.68 ms: sound reaching the far ear around the head
FRONT_CUTOFF, BACK_CUTOFF = 18000.0, 2200.0


# How loud each one-shot should sound, in dB of hearing-weighted loudness (loudest 300 ms).
# Gameplay cues are the loudest: getting hit, a roar announcing a monster, and the coin
# that tells you a kill landed.
LEVELS_DB = {
    "hurt": -12.0, "roar_low": -12.0, "roar_high": -12.0, "start": -12.0,
    "swoosh": -15.0, "coin": -12.0, "growl_low": -14.0, "growl_high": -14.0,
}


def _weighted_loudness_db(x) -> float:
    """Rough perceived loudness: drop the bass the ear barely hears, lift the 2-5 kHz it hears
    best (in the spirit of K/A-weighting), then take the RMS of the loudest 300 ms."""
    hp = sosfilt(butter(2, 120, "highpass", fs=SR, output="sos"), x)
    presence = sosfilt(butter(2, [1500, 6000], "bandpass", fs=SR, output="sos"), x)
    y = hp + 0.8 * presence
    win = min(len(y), int(0.3 * SR))
    power = np.convolve(y ** 2, np.ones(win) / win, mode="valid")
    return 10 * np.log10(power.max() + 1e-12)


def _limit(x, ceiling=0.95):
    """Squash only the peaks that would clip, leaving the body of the sound at full level."""
    win = int(0.005 * SR)
    env = maximum_filter1d(np.abs(x), size=2 * win + 1)       # looks 5 ms ahead and behind
    gain = np.minimum(1.0, ceiling / np.maximum(env, 1e-9))
    gain = np.minimum(gain, uniform_filter1d(gain, size=2 * win + 1))  # smooth it so it doesn't crackle
    return x * gain


def _level(x, target_db):
    out = x
    for _ in range(3):  # limiting loses a little loudness, so top up and limit again
        out = _limit(out * 10 ** ((target_db - _weighted_loudness_db(out)) / 20))
    return out.astype(np.float32)


class Voice:
    def __init__(self, buf, az=0.0, dist=1.0, gain=1.0, loop=False, spatial=True):
        self.buf = buf
        self.pos = 0
        self.az = az            # world azimuth, degrees clockwise from the camera
        self.dist = dist        # metres
        self.gain = gain
        self.loop = loop
        self.spatial = spatial
        self.done = False
        self._zi = np.zeros(1)
        self._hist = np.zeros(MAX_ITD, dtype=np.float32)
        self._prev = None       # last block's (left, right) gains, for click-free ramps

    def _take(self, n):
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        while filled < n:
            chunk = self.buf[self.pos:self.pos + n - filled]
            out[filled:filled + len(chunk)] = chunk
            filled += len(chunk)
            self.pos += len(chunk)
            if self.pos >= len(self.buf):
                if self.loop:
                    self.pos = 0
                else:
                    self.done = True
                    break
        return out

    def render(self, n, head_yaw):
        mono = self._take(n)
        if not self.spatial:
            g = self.gain
            return mono * g, mono * g
        rel = math.radians(wrap(self.az - head_yaw))
        s, c = math.sin(rel), math.cos(rel)
        back = max(0.0, -c)

        # Things behind you sound duller.
        fc = FRONT_CUTOFF * (BACK_CUTOFF / FRONT_CUTOFF) ** back
        a = 1.0 - math.exp(-2 * math.pi * fc / SR)
        mono, self._zi = lfilter([a], [1.0, a - 1.0], mono, zi=self._zi)
        mono = mono.astype(np.float32)

        # The far ear hears it a little later.
        delay = int(round(MAX_ITD * abs(s)))
        joined = np.concatenate([self._hist, mono])
        late = joined[MAX_ITD - delay:MAX_ITD - delay + n]
        self._hist = joined[-MAX_ITD:]

        # Equal-power level difference, never fully silent in the far ear (head shadow, not a wall).
        pan = 0.5 * (1 + s)
        gl, gr = math.cos(pan * math.pi / 2), math.sin(pan * math.pi / 2)
        gl, gr = 0.2 + 0.8 * gl, 0.2 + 0.8 * gr
        focus = 1.0 + 0.6 * max(0.0, (c - 0.9) / 0.1)        # looking straight at it brings it forward
        level = self.gain * focus * (1 - 0.35 * back) / (1 + 0.45 * max(0.0, self.dist - 1))
        gl, gr = gl * level, gr * level

        if self._prev is None:
            self._prev = (gl, gr)
        ramp = np.linspace(0, 1, n, dtype=np.float32)
        lg = self._prev[0] + (gl - self._prev[0]) * ramp
        rg = self._prev[1] + (gr - self._prev[1]) * ramp
        self._prev = (gl, gr)
        if s >= 0:   # source on the right: left ear is the far one
            return late * lg, mono * rg
        return mono * lg, late * rg


class AudioEngine:
    def __init__(self):
        self.head_yaw = 0.0
        self.master = 0.8
        self.voices: list[Voice] = []
        self.lock = threading.Lock()
        self.stream = None
        self.error = ""
        self.device_name = ""

        print("synthesizing sounds...")
        self.sfx = {
            "swoosh": sounds.swoosh(),
            # A few takes of each so repeated kills and hits don't sound canned.
            "coin": [sounds.coin() for _ in range(3)],
            "hurt": [sounds.hurt() for _ in range(3)],
            "roar_low": sounds.roar(80),
            "roar_high": sounds.roar(140, 1.0),
            "growl_low": sounds.growl_loop(60),
            "growl_high": sounds.growl_loop(110, 1.6),
            "start": sounds.taiko(60, 0.6),
        }
        for name, target in LEVELS_DB.items():
            v = self.sfx[name]
            self.sfx[name] = [_level(x, target) for x in v] if isinstance(v, list) else _level(v, target)
        # Music sits in the world in front of the camera ("north"): look away and it moves around you.
        self.drums = Voice(sounds.music_drums(), az=0, dist=3.0, gain=0.55, loop=True)
        self.flute = Voice(sounds.music_flute(), az=0, dist=3.0, gain=0.35, loop=True)
        self.drone = Voice(sounds.music_drone(), gain=0.18, loop=True, spatial=False)
        self.music = [self.drums, self.flute, self.drone]
        self.music_on = True

    def start(self):
        try:
            self.stream = sd.OutputStream(samplerate=SR, channels=2, blocksize=BLOCK,
                                          dtype="float32", callback=self._callback, latency="low")
            self.stream.start()
            self.device_name = sd.query_devices(self.stream.device)["name"]
        except Exception as e:
            self.error = str(e)

    def close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def play(self, name, az=0.0, dist=1.0, gain=1.0, loop=False, spatial=True, delay=0.0) -> Voice:
        buf = self.sfx[name]
        if isinstance(buf, list):
            buf = random.choice(buf)
        if delay > 0:
            buf = np.concatenate([np.zeros(int(delay * SR), dtype=np.float32), buf])
        v = Voice(buf, az, dist, gain, loop, spatial)
        with self.lock:
            self.voices.append(v)
        return v

    def play_kill(self, az, dist):
        """A zombie cut down: just the coin it drops, from where it fell."""
        return self.play("coin", az=az, dist=min(dist, 1.5))

    def stop(self, voice: Voice):
        voice.done = True

    def _callback(self, out, frames, _time, _status):
        head = self.head_yaw
        left = np.zeros(frames, dtype=np.float32)
        right = np.zeros(frames, dtype=np.float32)
        with self.lock:
            voices = [v for v in self.voices if not v.done]
            self.voices = voices
        if self.music_on:
            voices = self.music + voices
        for v in voices:
            l, r = v.render(frames, head)
            left += l
            right += r
        out[:, 0] = np.tanh(left * self.master)
        out[:, 1] = np.tanh(right * self.master)

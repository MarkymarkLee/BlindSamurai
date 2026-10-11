"""All game audio is synthesized at startup, so there are no asset files to ship."""
import numpy as np
from scipy import signal

SR = 48000
rng = np.random.default_rng(7)


def _t(seconds):
    return np.arange(int(SR * seconds)) / SR


def _env(n, attack=0.005, decay=0.3):
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t / decay)


def _bandpass(x, lo, hi, order=2):
    sos = signal.butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def _lowpass(x, fc, order=2):
    return signal.sosfilt(signal.butter(order, fc, fs=SR, output="sos"), x)


def _norm(x, peak=0.9):
    m = np.max(np.abs(x))
    return (x / m * peak if m > 0 else x).astype(np.float32)


def _place(buf, sound, at):
    i = int(at * SR)
    j = min(len(buf), i + len(sound))
    buf[i:j] += sound[: j - i]


# ---------- one-shots ----------

def taiko(freq=70.0, decay=0.35):
    n = int(SR * (decay * 4))
    t = np.arange(n) / SR
    pitch = freq * (1 + 0.8 * np.exp(-t / 0.03))
    body = np.sin(2 * np.pi * np.cumsum(pitch) / SR) * _env(n, 0.002, decay)
    slap = _bandpass(rng.standard_normal(n), 800, 3000) * _env(n, 0.001, 0.02) * 0.4
    return _norm(body + slap)


def swoosh():
    t = _t(0.35)
    noise = rng.standard_normal(len(t))
    # Bands of noise lighting up one after another, low to high: a blade cutting the air.
    out = np.zeros_like(noise)
    centers = np.geomspace(600, 6000, 6)
    for k, fc in enumerate(centers):
        when = 0.06 + 0.04 * k
        out += _bandpass(noise, fc * 0.7, fc * 1.4) * np.exp(-((t - when) / 0.05) ** 2)
    return _norm(out, 0.7)


def _formants(x, formants):
    """Shape a buzzy source into a vowel: a sum of resonances (centre Hz, bandwidth Hz, gain)."""
    return sum(_bandpass(x, f - bw / 2, f + bw / 2) * g for f, bw, g in formants)


def _voice(f0_start, f0_end, seconds, formants, jitter=0.03, breath=0.3, r=rng):
    """A throat: a sawtooth with a gliding, wobbling pitch, through vowel formants, plus breath."""
    t = _t(seconds)
    glide = f0_start * (f0_end / f0_start) ** (t / seconds)
    wobble = 1 + jitter * _lowpass(r.standard_normal(len(t)), 12) * 8
    src = signal.sawtooth(2 * np.pi * np.cumsum(glide * wobble) / SR)
    src = src + breath * r.standard_normal(len(t))
    return _formants(src, formants)


def hurt(r=rng):
    """A zombie gets you. Dark and inside your head: a muffled blow, your grunt and your heart
    pounding, nothing like the bright clink of the coin you get for a kill."""
    out = np.zeros(int(SR * 1.8))
    t = _t(0.6)
    blow = np.sin(2 * np.pi * (38 + 60 * np.exp(-t / 0.02)) * t) * np.exp(-t / 0.18)
    blow += _lowpass(r.standard_normal(len(t)), 250) * np.exp(-t / 0.04) * 0.9
    crunch = _bandpass(r.standard_normal(int(SR * 0.2)), 500, 1800) * (r.random(int(SR * 0.2)) > 0.9)
    crunch = crunch * np.exp(-_t(0.2) / 0.06)
    grunt_s = 0.35
    grunt = _voice(r.uniform(150, 175), 110, grunt_s, [(600, 200, 1.0), (1100, 250, 0.5), (2400, 400, 0.15)],
                   jitter=0.02, breath=0.2, r=r)
    grunt *= np.clip(_t(grunt_s) / 0.02, 0, 1) * np.exp(-_t(grunt_s) / 0.14)
    _place(out, blow * 1.3, 0.0)
    _place(out, crunch * 0.5, 0.02)
    _place(out, _lowpass(_norm(grunt), 1800) * 0.6, 0.06)
    # Heartbeat: two lub-dubs, fast.
    beat = lambda f: np.sin(2 * np.pi * f * _t(0.12)) * np.exp(-_t(0.12) / 0.035)
    for at in (0.55, 1.0):
        _place(out, beat(48) * 0.9, at)
        _place(out, beat(42) * 0.6, at + 0.14)
    # Everything muffled, as if heard through your own skull, and driven into saturation so the
    # blow has grit you can hear on small earbuds that barely reproduce the low thud itself.
    return _norm(_lowpass(np.tanh(3.0 * _norm(_lowpass(out, 1200))), 2200))


def _clink(f0, decay, r=rng):
    """One small metal coin struck: a few inharmonic plate modes ringing out briefly."""
    t = _t(decay * 5)
    modes = [(1.0, 1.0), (2.76, 0.4), (5.40, 0.12)]
    ring = sum(np.sin(2 * np.pi * f0 * m * t + r.uniform(0, 6.28)) * g * np.exp(-t * m ** 0.5 / decay)
               for m, g in modes)
    tick = _bandpass(r.standard_normal(len(t)), 2000, 6000) * np.exp(-t / 0.002) * 0.4
    return ring + tick


def coin(r=rng):
    """Picking up the zombie's coin: it clinks on the ground, then a few coins jingle in your hand.
    Metal on metal like the rest of the world, not a video-game blip."""
    out = np.zeros(int(SR * 0.9))
    # Dropped coin bouncing: hits getting quicker and quieter.
    at, gap, level = 0.0, 0.11, 1.0
    f0 = r.uniform(1500, 1800)
    for _ in range(4):
        _place(out, _clink(f0, 0.09, r) * level, at)
        at += gap
        gap *= 0.6
        level *= 0.55
    # Scooped into your purse: a short shake of several coins.
    for _ in range(7):
        _place(out, _clink(r.uniform(1300, 2200), 0.05, r) * r.uniform(0.25, 0.5), 0.42 + r.uniform(0, 0.22))
    rustle = _bandpass(r.standard_normal(int(SR * 0.3)), 1000, 4000) * np.sin(np.pi * _t(0.3) / 0.3) * 0.06
    _place(out, rustle, 0.42)
    return _norm(out)


def roar(base=90.0, seconds=1.4):
    """Monster announcing itself when it appears."""
    t = _t(seconds)
    f = base * (1 + 0.3 * np.sin(2 * np.pi * 0.9 * t)) * (1 - 0.25 * t / seconds)
    saw = signal.sawtooth(2 * np.pi * np.cumsum(f) / SR)
    rough = _bandpass(rng.standard_normal(len(t)), 200, 1500) * (0.5 + 0.5 * np.sin(2 * np.pi * 23 * t))
    x = _lowpass(saw * 0.7 + rough, 1800)
    return _norm(x * np.sin(np.pi * t / seconds) ** 0.6)


def growl_loop(base=70.0, seconds=2.0):
    """Looping breath + footsteps for an approaching monster; seamless at the loop point."""
    t = _t(seconds)
    breath_rate = 2.0 / seconds  # two breaths per loop
    breath = 0.5 - 0.5 * np.cos(2 * np.pi * breath_rate * t)
    f = base * (1 + 0.08 * np.sin(2 * np.pi * breath_rate * t))
    voice = signal.sawtooth(2 * np.pi * np.cumsum(f) / SR)
    noise = _bandpass(rng.standard_normal(len(t)), 150, 2500)
    x = _lowpass(voice * 0.5 + noise * 0.8, 1400) * (0.15 + breath)
    steps = np.zeros_like(t)
    for k in range(4):
        _place(steps, taiko(48, 0.08) * 0.8, k * seconds / 4)
    out = x + steps
    fade = min(len(out) // 50, 2000)  # crossfade the seam
    out[:fade] = out[:fade] * np.linspace(0, 1, fade) + out[-fade:] * np.linspace(1, 0, fade)
    return _norm(out[:-fade] if fade else out, 0.8)


# ---------- music stems (all the same length so they stay in sync) ----------

BPM = 96
BEAT = 60 / BPM
BARS = 4
LOOP_S = BARS * 4 * BEAT


def music_drums():
    buf = np.zeros(int(SR * LOOP_S))
    pattern = [0, 1.5, 2, 3, 3.5]  # beats within a bar
    for bar in range(BARS):
        for b in pattern:
            _place(buf, taiko(68 if b in (0, 2) else 95, 0.3) * (1.0 if b in (0, 2) else 0.55), (bar * 4 + b) * BEAT)
        _place(buf, taiko(140, 0.05) * 0.3, (bar * 4 + 3.75) * BEAT)  # rim click
    return _norm(buf, 0.8)


def music_flute():
    """Shakuhachi-ish line on a minor pentatonic (in-sen-like) scale."""
    buf = np.zeros(int(SR * LOOP_S))
    scale = [293.66, 311.13, 392.0, 440.0, 466.16, 587.33]  # D Eb G A Bb D
    phrase = [(0, 2, 2.0), (2, 4, 1.0), (3, 3, 1.0), (4.5, 1, 1.5), (6, 2, 2.0),
              (8, 5, 2.0), (10, 4, 1.0), (11, 2, 1.0), (12, 3, 1.5), (13.5, 2, 2.5)]
    for start, note, beats in phrase:
        dur = beats * BEAT
        t = _t(dur)
        f = scale[note] * (1 + 0.006 * np.sin(2 * np.pi * 5.2 * t) * np.clip(t / 0.4, 0, 1))
        tone = np.sin(2 * np.pi * np.cumsum(f) / SR) + 0.25 * np.sin(4 * np.pi * np.cumsum(f) / SR)
        breath = _bandpass(rng.standard_normal(len(t)), scale[note] * 1.5, scale[note] * 4) * 0.25
        env = np.clip(t / 0.12, 0, 1) * np.clip((dur - t) / 0.25, 0, 1)
        _place(buf, (tone + breath) * env, start * BEAT)
    return _norm(buf, 0.6)


def music_drone():
    t = _t(LOOP_S)
    # Snap each partial to a whole number of cycles per loop so the seam doesn't click.
    snap = lambda f: round(f * LOOP_S) / LOOP_S
    d = sum(np.sin(2 * np.pi * snap(f) * t + p) * a for f, p, a in ((73.42, 0, 1.0), (110.0, 1.0, 0.5), (146.83, 2.0, 0.3)))
    slow = 0.7 + 0.3 * np.sin(2 * np.pi * t / LOOP_S)  # whole cycles per loop, so it loops cleanly
    return _norm(d * slow, 0.5)

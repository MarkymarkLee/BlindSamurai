"""Sound test board: play every game sound from any direction and distance.

Run: ./run.sh --sound-test
"""
import math
import time

import pygame

from . import sounds, wrap
from .audio import AudioEngine
from .head import HeadTracker

W, H = 940, 650
BG = (14, 14, 18)
PANEL = (24, 25, 31)
INK = (230, 230, 225)
MUTED = (130, 130, 125)
LINE = (55, 57, 66)
ACCENT = (255, 150, 40)
HEAD_COL = (60, 200, 255)
SRC_COL = (230, 50, 60)

# key, label, sfx name, spatial, loops
SOUNDS = [
    (pygame.K_1, "Kill zombie (coin)", "kill", True, False),
    (pygame.K_2, "You get hit", "hurt", False, False),
    (pygame.K_3, "Sword swoosh", "swoosh", True, False),
    (pygame.K_4, "Roar (low)", "roar_low", True, False),
    (pygame.K_5, "Roar (high)", "roar_high", True, False),
    (pygame.K_6, "Growl loop (low)", "growl_low", True, True),
    (pygame.K_7, "Growl loop (high)", "growl_high", True, True),
    (pygame.K_8, "Start drum", "start", False, False),
]
COMPASS_C = (660, 270)
COMPASS_R = 170
MAX_DIST = 10.0


class SoundTest:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Blind Samurai: sound test")
        self.screen = pygame.display.set_mode((W, H))
        self.font = pygame.font.SysFont("Menlo", 15)
        self.big = pygame.font.SysFont("Menlo", 26, bold=True)
        self.clock = pygame.time.Clock()
        self.audio = AudioEngine()
        self.audio.music_on = False
        self.audio.start()
        self.head = HeadTracker()
        self.head.start()
        self.az = 45.0       # world azimuth of the test source
        self.dist = 3.0
        self.sim_head = 0.0
        self.loops = {}      # sfx name -> playing Voice
        self.last = ""
        self.buttons = {}
        y = 90
        for key, label, name, spatial, loops in SOUNDS:
            self.buttons[name] = pygame.Rect(30, y, 300, 38)
            y += 44
        self.music_btn = pygame.Rect(30, y + 10, 145, 40)
        self.regen_btn = pygame.Rect(185, y + 10, 145, 40)
        self.dist_slider = pygame.Rect(COMPASS_C[0] - 170, 540, 340, 12)
        self.drag_compass = self.drag_slider = False

    @property
    def head_yaw(self):
        return self.head.yaw if self.head.active else self.sim_head

    def play(self, name):
        spec = next(s for s in SOUNDS if s[2] == name)
        _, label, _, spatial, loops = spec
        if loops:
            if name in self.loops:
                self.audio.stop(self.loops.pop(name))
                self.last = f"{label}: stopped"
                return
            self.loops[name] = self.audio.play(name, az=self.az, dist=self.dist, gain=1.4, loop=True)
        elif name == "kill":
            self.audio.play_kill(self.az, self.dist)
        else:
            self.audio.play(name, az=self.az, dist=self.dist, spatial=spatial)
        where = self.where() if spatial else "inside your head (not positioned)"
        self.last = f"{label}: {where}"

    def where(self):
        rel = wrap(self.az - self.head_yaw)
        lr = "right" if rel > 0 else "left"
        a = abs(rel)
        side = ("straight ahead" if a < 15 else f"ahead-{lr}" if a < 70 else lr if a <= 110
                else f"behind-{lr}" if a <= 165 else "straight behind")
        return f"{side} ({a:.0f}°) of your head, {self.dist:.1f} m"

    def regenerate(self):
        from .audio import LEVELS_DB, _level
        self.audio.sfx["coin"] = [_level(sounds.coin(), LEVELS_DB["coin"]) for _ in range(3)]
        self.audio.sfx["hurt"] = [_level(sounds.hurt(), LEVELS_DB["hurt"]) for _ in range(3)]
        self.last = "made 3 new takes of the coin and the hit"

    # ---------- input ----------
    def handle(self, e):
        if e.type == pygame.KEYDOWN:
            for key, _, name, _, _ in SOUNDS:
                if e.key == key:
                    self.play(name)
            if e.key == pygame.K_m:
                self.audio.music_on = not self.audio.music_on
            elif e.key == pygame.K_n:
                self.regenerate()
            elif e.key == pygame.K_c:
                self.head.calibrate()
                self.sim_head = 0.0
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            for name, rect in self.buttons.items():
                if rect.collidepoint(e.pos):
                    self.play(name)
                    return
            if self.music_btn.collidepoint(e.pos):
                self.audio.music_on = not self.audio.music_on
            elif self.regen_btn.collidepoint(e.pos):
                self.regenerate()
            elif self.dist_slider.inflate(0, 24).collidepoint(e.pos):
                self.drag_slider = True
                self._slide(e.pos[0])
            elif math.dist(e.pos, COMPASS_C) <= COMPASS_R + 10:
                self.drag_compass = True
                self._aim(e.pos)
        elif e.type == pygame.MOUSEBUTTONUP:
            self.drag_compass = self.drag_slider = False
        elif e.type == pygame.MOUSEMOTION:
            if self.drag_compass:
                self._aim(e.pos)
            elif self.drag_slider:
                self._slide(e.pos[0])

    def _aim(self, pos):
        # The compass is drawn head-up, so a click means "relative to where I'm looking".
        rel = math.degrees(math.atan2(pos[0] - COMPASS_C[0], COMPASS_C[1] - pos[1]))
        self.az = wrap(self.head_yaw + rel)

    def _slide(self, x):
        frac = max(0.0, min(1.0, (x - self.dist_slider.x) / self.dist_slider.w))
        self.dist = round(0.5 + frac * (MAX_DIST - 0.5), 1)

    # ---------- drawing ----------
    def text(self, s, pos, col=INK, font=None, center=False):
        surf = (font or self.font).render(s, True, col)
        r = surf.get_rect(center=pos) if center else surf.get_rect(topleft=pos)
        self.screen.blit(surf, r)

    def button(self, rect, label, hot=False):
        pygame.draw.rect(self.screen, ACCENT if hot else LINE, rect, border_radius=6)
        self.text(label, (rect.x + 12, rect.centery - 8), (20, 20, 20) if hot else INK)

    def draw(self):
        self.screen.fill(BG)
        self.text("SOUND TEST", (30, 28), INK, self.big)
        for key, label, name, spatial, loops in SOUNDS:
            hot = name in self.loops
            self.button(self.buttons[name], f"{pygame.key.name(key)}  {label}{'  (stop)' if hot else ''}", hot)
        self.button(self.music_btn, f"m  Music {'on' if self.audio.music_on else 'off'}", self.audio.music_on)
        self.button(self.regen_btn, "n  New takes")

        # Compass, head-up: top of the circle is where you're looking.
        c = COMPASS_C
        pygame.draw.circle(self.screen, PANEL, c, COMPASS_R)
        for frac in (0.25, 0.5, 0.75, 1.0):
            pygame.draw.circle(self.screen, LINE, c, int(COMPASS_R * frac), 1)
        self.text("AHEAD", (c[0], c[1] - COMPASS_R - 14), MUTED, center=True)
        self.text("BEHIND", (c[0], c[1] + COMPASS_R + 14), MUTED, center=True)
        self.text("L", (c[0] - COMPASS_R - 16, c[1]), MUTED, center=True)
        self.text("R", (c[0] + COMPASS_R + 16, c[1]), MUTED, center=True)
        pygame.draw.circle(self.screen, (60, 60, 70), c, 16)
        pygame.draw.line(self.screen, HEAD_COL, c, (c[0], c[1] - 40), 3)
        rel = math.radians(wrap(self.az - self.head_yaw))
        r = COMPASS_R * self.dist / MAX_DIST
        src = (c[0] + r * math.sin(rel), c[1] - r * math.cos(rel))
        pygame.draw.line(self.screen, LINE, c, src, 1)
        pygame.draw.circle(self.screen, SRC_COL, src, 11)
        self.text("click or drag to place the sound", (c[0], c[1] + COMPASS_R + 40), MUTED, center=True)

        s = self.dist_slider
        self.text(f"Distance {self.dist:.1f} m", (s.x, s.y - 26), INK)
        pygame.draw.rect(self.screen, LINE, s, border_radius=6)
        frac = (self.dist - 0.5) / (MAX_DIST - 0.5)
        pygame.draw.rect(self.screen, ACCENT, (s.x, s.y, int(s.w * frac), s.h), border_radius=6)
        pygame.draw.circle(self.screen, INK, (s.x + int(s.w * frac), s.centery), 10)

        head = f"AirPods head {self.head.yaw:+.0f}° (c recenters)" if self.head.active else \
            f"AirPods: {self.head.status}. a/d turn a pretend head ({self.sim_head:+.0f}°)"
        self.text(head, (30, H - 56), HEAD_COL if self.head.active else MUTED)
        self.text(self.last, (30, H - 32), ACCENT)
        self.text(f"output: {self.audio.error or self.audio.device_name}", (560, H - 32), MUTED)
        pygame.display.flip()

    def run(self):
        try:
            while True:
                dt = self.clock.tick(60) / 1000.0
                for e in pygame.event.get():
                    if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q)):
                        return
                    self.handle(e)
                keys = pygame.key.get_pressed()
                self.sim_head = wrap(self.sim_head + 120 * dt * (keys[pygame.K_d] - keys[pygame.K_a]))
                self.audio.head_yaw = self.head_yaw
                # Loops follow the marker while you move it.
                for v in self.loops.values():
                    v.az, v.dist = self.az, self.dist
                self.draw()
        finally:
            self.audio.close()
            pygame.quit()


def main():
    SoundTest().run()

"""Blind Samurai game loop and display."""
import argparse
import math
import os
import random
import time
from dataclasses import dataclass, field

import pygame

from . import wrap
from .audio import AudioEngine, Voice
from .dotsetup import DotSetup
from .haptics import ROLE_ANGLES, ROLES, THERMAL_COLD_C, THERMAL_HOT_C, Haptics, direction_weights
from .head import HeadTracker
from .pose import SKELETON, PoseTracker, Slash, default_camera, ensure_camera_access, list_cameras

W, H = 1320, 780
RADAR_C = (390, 410)
RADAR_R = 330
SPAWN_DIST = 10.0     # metres
STRIKE_RANGE = 2.5    # a slash reaches this far...
SLASH_ARC = 35.0      # ...within this many degrees either side of where your head (AirPods) points
HIT_DIST = 0.6        # monster gets you
LIVES = 5
CHALLENGES_PER_WAVE = 3   # monsters per wave

BG = (14, 14, 18)
INK = (230, 230, 225)
MUTED = (130, 130, 125)
GRID = (40, 42, 50)
BODY_COL = (255, 150, 40)
HEAD_COL = (60, 200, 255)
MONSTER_COL = (230, 50, 60)
OK_COL = (80, 210, 120)
WARN_COL = (240, 190, 60)


@dataclass
class Monster:
    az: float
    dist: float
    speed: float
    kind: str
    voice: Voice
    born: float = field(default_factory=time.monotonic)


def in_sweep(az, start, end, arc=SLASH_ARC) -> bool:
    """Is az within arc of the path a sword swept from start to end (shortest way round)?"""
    span = wrap(end - start)
    rel = wrap(az - start)
    if span < 0:
        span, rel = -span, -rel
    return -arc <= rel <= span + arc


def polar(az, dist, scale=RADAR_R / SPAWN_DIST, center=RADAR_C):
    r = dist * scale
    a = math.radians(az)
    return center[0] + r * math.sin(a), center[1] - r * math.cos(a)


class Game:
    def __init__(self, args):
        pygame.init()
        pygame.display.set_caption("Blind Samurai")
        self.screen = pygame.display.set_mode((W, H))
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("Menlo", 15)
        self.big = pygame.font.SysFont("Menlo", 30, bold=True)
        self.huge = pygame.font.SysFont("Menlo", 44, bold=True)

        self.audio = AudioEngine()
        self.audio.start()
        # The helper also does voice commands, so it runs even when AirPods tracking is ignored.
        self.use_airpods = not args.no_airpods
        self.head = HeadTracker()
        self.head.start()
        self.haptics = Haptics(enabled=not args.no_haptics, intensity=args.haptic_intensity)
        self.haptics.start()
        self.setup = DotSetup(self.screen, self.haptics, self.font, self.big)
        self.setup_btn = pygame.Rect(0, 0, 0, 0)

        self.cameras = list_cameras()
        cam = args.camera if args.camera is not None else default_camera()
        self.pose = None
        if not args.no_camera:
            access = ensure_camera_access()
            if access != "authorized":
                print(f"camera access is {access}: allow your terminal in System Settings > Privacy & Security > Camera")
            self.pose = PoseTracker(cam, mirror=True)
            self.pose.start()

        self.sim_body = 0.0
        self.sim_head = 0.0
        self.body_yaw = 0.0
        self.head_yaw = 0.0
        self.body_source = "keyboard"
        self.head_source = "body"
        self.last_slash: Slash | None = None
        self.slash_fx = 0.0
        self.kill_fx: list[tuple[float, float, float]] = []   # (az, dist, time)
        self.hurt_fx = 0.0
        self.reset()
        self.state = "ready"

    # ---------- game state ----------
    def reset(self):
        for m in getattr(self, "monsters", []):
            self.audio.stop(m.voice)
        self.monsters: list[Monster] = []
        self.score = 0
        self.lives = LIVES
        self.wave = 1
        self.challenges_done = 0
        self.banner = ("", 0.0)
        self.next_spawn = time.monotonic() + 2.0

    def start(self):
        self.reset()
        self.state = "playing"
        self.audio.play("start", spatial=False)

    def spawn(self, az, kind, dist, speed):
        voice = self.audio.play(f"growl_{kind}", az=az, dist=dist, gain=1.4, loop=True)
        self.audio.play(f"roar_{kind}", az=az, dist=min(dist, SPAWN_DIST) * 0.6, gain=1.2)
        self.monsters.append(Monster(az, dist, speed, kind, voice))

    def spawn_challenge(self):
        """One monster at a time, from a random direction; each wave it comes in faster."""
        w = self.wave
        kind = random.choice(["low", "high"]) if w >= 2 else "low"
        speed = (0.6 + 0.1 * (w - 1)) * (1.2 if kind == "high" else 1.0)
        self.spawn(random.uniform(-180, 180), kind, SPAWN_DIST, speed)
        self.banner = (f"MONSTER {self.challenges_done + 1}", time.monotonic())

    def challenge_over(self, now):
        self.challenges_done += 1
        self.wave = 1 + self.challenges_done // CHALLENGES_PER_WAVE
        self.next_spawn = now + 2.5

    def slash(self, s: Slash | None):
        now = time.monotonic()
        if s is not None:
            self.last_slash = s
        self.slash_fx = now
        self.audio.play("swoosh", az=self.head_yaw, dist=0.8, gain=0.9 if s is None or not s.sweep else 1.1)
        self.haptics.slash()
        if self.state != "playing":
            self.start()
            return
        # The sword cuts where you look: AirPods head direction aims it. A head swing also
        # counts if it sweeps across the monster on the way, not just where it stops.
        sweep = s.sweep if s is not None and s.sweep else (self.head_yaw, self.head_yaw)
        in_reach = [m for m in self.monsters if m.dist <= STRIKE_RANGE and in_sweep(m.az, *sweep)]
        if not in_reach:
            return
        for m in in_reach:
            self.monsters.remove(m)
            self.audio.stop(m.voice)
            self.audio.play_kill(m.az, m.dist)
            self.kill_fx.append((m.az, m.dist, now))
            self.score += 2 if m.kind == "high" else 1
        self.haptics.kill()
        if not self.monsters:
            self.challenge_over(now)

    def update(self, dt):
        now = time.monotonic()
        keys = pygame.key.get_pressed()
        turn = 120 * dt
        self.sim_body = wrap(self.sim_body + turn * (keys[pygame.K_RIGHT] - keys[pygame.K_LEFT]))
        self.sim_head = max(-90, min(90, self.sim_head + turn * (keys[pygame.K_d] - keys[pygame.K_a])))

        # Body: camera if it sees someone, else arrow keys.
        if self.pose and self.pose.detected and now - self.pose.last_seen < 1.0:
            self.body_yaw, self.body_source = self.pose.body_yaw, "camera"
        else:
            self.body_yaw, self.body_source = self.sim_body, "keyboard"
        # Head: AirPods if streaming, else body + A/D.
        if self.use_airpods and self.head.active:
            self.head_yaw, self.head_source = self.head.yaw, "AirPods"
        else:
            self.head_yaw, self.head_source = wrap(self.body_yaw + self.sim_head), "keyboard"
        self.audio.head_yaw = self.head_yaw

        while not self.head.commands.empty():
            cmd = self.head.commands.get()
            if cmd == "start game" and self.state != "playing":
                self.start()
            elif cmd == "stop game" and self.state == "playing":
                self.reset()
                self.state = "ready"
        while not self.head.slashes.empty():
            s = self.head.slashes.get()
            if self.use_airpods and not self.setup.open:
                self.slash(s)
        if self.pose:
            while not self.pose.slashes.empty():
                s = self.pose.slashes.get()
                if not self.setup.open:
                    self.slash(s)

        threat = 0.0
        levels = {r: 0.0 for r in ROLES}
        if self.state == "playing" and not self.setup.open:   # the setup screen pauses the game
            if not self.monsters and now >= self.next_spawn:
                self.spawn_challenge()
            for m in list(self.monsters):
                m.dist -= m.speed * dt
                m.voice.dist = m.dist
                if m.dist <= HIT_DIST:
                    self.monsters.remove(m)
                    self.audio.stop(m.voice)
                    self.audio.play("hurt", spatial=False)
                    self.haptics.hit()
                    self.hurt_fx = now
                    self.lives -= 1
                    if self.lives <= 0:
                        self.state = "over"
                        for o in self.monsters:
                            self.audio.stop(o.voice)
                        self.monsters = []
                        break
                    if not self.monsters:
                        self.challenge_over(now)
            for m in self.monsters:
                prox = self.proximity(m)
                # Strong from the moment it appears, full power once it's within reach.
                level = 1.0 if m.dist <= STRIKE_RANGE else 0.55 + 0.45 * prox
                threat = max(threat, prox)
                for r, w in direction_weights(wrap(m.az - self.body_yaw)).items():
                    if w >= 0.25:
                        levels[r] = max(levels[r], level * (0.6 + 0.4 * w))
            # Wrist: the closest monster. Vibration and heat both rise as it closes in;
            # full vibration means it's in reach and your head is on it, so strike.
            closest = min(self.monsters, key=lambda m: m.dist, default=None)
            if closest:
                prox = self.proximity(closest)
                aimed = closest.dist <= STRIKE_RANGE and abs(wrap(closest.az - self.head_yaw)) <= SLASH_ARC
                levels["wrist"] = 1.0 if aimed else 0.4 + 0.4 * prox
                self.haptics.set_wrist_temp(THERMAL_COLD_C + (THERMAL_HOT_C - THERMAL_COLD_C) * prox)
            else:
                self.haptics.set_wrist_temp(None)
        else:
            self.haptics.set_wrist_temp(None)
        self.haptics.set_targets(levels)
        # The drums push harder as the nearest monster closes in.
        self.audio.drums.gain = 0.45 + 0.5 * threat

    @staticmethod
    def proximity(m) -> float:
        """0 when a monster is at spawn distance (or further), 1 when it's within reach."""
        return max(0.0, min(1.0, (SPAWN_DIST - m.dist) / (SPAWN_DIST - STRIKE_RANGE)))

    # ---------- drawing ----------
    def text(self, s, pos, col=INK, font=None, center=False):
        surf = (font or self.font).render(s, True, col)
        r = surf.get_rect()
        if center:
            r.center = pos
        else:
            r.topleft = pos
        self.screen.blit(surf, r)
        return r.bottom

    def draw_radar(self):
        now = time.monotonic()
        c = RADAR_C
        for d in (2.5, 5, 7.5, 10):
            pygame.draw.circle(self.screen, GRID, c, int(d * RADAR_R / SPAWN_DIST), 1)
        pygame.draw.circle(self.screen, (70, 40, 40), c, int(STRIKE_RANGE * RADAR_R / SPAWN_DIST), 2)
        for a in range(0, 360, 45):
            pygame.draw.line(self.screen, GRID, c, polar(a, SPAWN_DIST), 1)
        self.text("CAMERA", (c[0], c[1] - RADAR_R - 14), MUTED, center=True)
        for label, a in (("R", 90), ("BEHIND", 180), ("L", -90)):
            x, y = polar(a, SPAWN_DIST * 1.05)
            self.text(label, (x, y), MUTED, center=True)

        # Slash reach: wedge where the head (the sword) points.
        pts = [c] + [polar(self.head_yaw + a, STRIKE_RANGE) for a in range(int(-SLASH_ARC), int(SLASH_ARC) + 1, 5)]
        flash = max(0.0, 1 - (now - self.slash_fx) / 0.25)
        wedge = pygame.Surface((W, H), pygame.SRCALPHA)
        pygame.draw.polygon(wedge, (*HEAD_COL, int(50 + 150 * flash)), pts)
        self.screen.blit(wedge, (0, 0))

        for m in self.monsters:
            x, y = polar(m.az, m.dist)
            r = 13 if m.kind == "low" else 10
            in_reach = m.dist <= STRIKE_RANGE
            if in_reach:
                pygame.draw.line(self.screen, (120, 30, 30), c, (x, y), 1)
            pygame.draw.circle(self.screen, MONSTER_COL, (x, y), r + (4 if in_reach else 0))
            pygame.draw.circle(self.screen, (255, 200, 200), (x, y), r + (4 if in_reach else 0), 2)
        for az, dist, t in list(self.kill_fx):
            age = now - t
            if age > 0.6:
                self.kill_fx.remove((az, dist, t))
                continue
            pygame.draw.circle(self.screen, (255, 255, 255), polar(az, dist), int(10 + 60 * age), 2)

        # Player, body and head.
        pygame.draw.circle(self.screen, (60, 60, 70), c, 26)
        pygame.draw.line(self.screen, BODY_COL, c, polar(self.body_yaw, 1.9), 6)
        pygame.draw.line(self.screen, HEAD_COL, c, polar(self.head_yaw, 2.3), 3)
        pygame.draw.circle(self.screen, HEAD_COL, polar(self.head_yaw, 2.3), 5)

        # Haptic dots around the torso, rotating with the body.
        # The wrist dot is drawn out by the sword hand, in front and to the right.
        spots = dict(ROLE_ANGLES, wrist=40.0)
        for role in ROLES:
            x, y = polar(self.body_yaw + spots[role], 1.2 if role != "wrist" else 1.75)
            lvl = self.haptics.target.get(role, 0.0)
            col = (int(60 + 195 * lvl), int(60 + 40 * lvl), int(70 - 30 * lvl))
            size = 9 if role != "wrist" else 7
            pygame.draw.circle(self.screen, col, (x, y), size)
            ring = OK_COL if self.haptics.address(role) in self.haptics.connected else (90, 90, 90)
            pygame.draw.circle(self.screen, ring, (x, y), size, 2)

        if now - self.hurt_fx < 0.4:
            red = pygame.Surface((W, H), pygame.SRCALPHA)
            red.fill((200, 0, 0, int(120 * (1 - (now - self.hurt_fx) / 0.4))))
            self.screen.blit(red, (0, 0))

    def draw_camera(self, x0, y0, w):
        if not self.pose:
            self.text("camera disabled", (x0, y0), MUTED)
            return y0 + 20
        with self.pose.lock:
            frame, lms = self.pose.frame, self.pose.landmarks
        if frame is None:
            self.text(self.pose.error or "opening camera... (allow camera access if asked)", (x0, y0), WARN_COL)
            return y0 + 20
        fh, fw = frame.shape[:2]
        h = int(w * fh / fw)
        surf = pygame.image.frombuffer(frame.tobytes(), (fw, fh), "RGB")
        self.screen.blit(pygame.transform.smoothscale(surf, (w, h)), (x0, y0))
        if lms:
            pts = [(x0 + p[0] * w, y0 + p[1] * h) for p in lms]
            for i, j in SKELETON:
                if lms[i][2] > 0.4 and lms[j][2] > 0.4:
                    pygame.draw.line(self.screen, OK_COL, pts[i], pts[j], 2)
            for i in (15, 16):
                if lms[i][2] > 0.4:
                    pygame.draw.circle(self.screen, BODY_COL, pts[i], 7)
        pygame.draw.rect(self.screen, GRID, (x0, y0, w, h), 1)
        return y0 + h

    def draw_panel(self):
        x = 790
        y = self.draw_camera(x, 20, 500) + 12
        cam_name = ""
        if self.pose and self.pose.camera < len(self.cameras):
            cam_name = self.cameras[self.pose.camera]
        if self.pose:
            seen = "person tracked" if self.pose.detected else "no person"
            y = self.text(f"Camera {self.pose.camera} {cam_name}  {self.pose.fps:4.1f} fps  {seen}", (x, y),
                          OK_COL if self.pose.detected else WARN_COL)
        y = self.text(f"Body facing  {self.body_yaw:+6.0f}°   ({self.body_source})", (x, y + 4), BODY_COL)
        y = self.text(f"Head facing  {self.head_yaw:+6.0f}°   ({self.head_source})", (x, y), HEAD_COL)
        y = self.text(f"Neck turn    {wrap(self.head_yaw - self.body_yaw):+6.0f}°", (x, y), MUTED)
        y = self.text(f"AirPods      {self.head.status if self.use_airpods else 'ignored'}", (x, y + 6),
                      OK_COL if self.head.active else WARN_COL)
        heard = f'  heard "{self.head.heard[-40:]}"' if self.head.heard else ""
        y = self.text(f"Voice        {self.head.voice_status}{heard}", (x, y),
                      OK_COL if self.head.voice_status == "listening" else WARN_COL)
        audio = self.audio.error or self.audio.device_name
        y = self.text(f"Audio out    {audio}", (x, y), WARN_COL if self.audio.error else MUTED)
        y = self.text(f"Haptics      {self.haptics.status}   strength {self.haptics.intensity:.0%} ([ / ])", (x, y),
                      OK_COL if self.haptics.connected else WARN_COL)
        temp = self.haptics.wrist_temp
        y = self.text(f"Wrist heat   {f'{temp:.0f}°C (cold = far, hot = near)' if temp is not None else 'off'}",
                      (x, y), MUTED)
        bx = x
        for role in ROLES:
            lvl = self.haptics.target.get(role, 0.0)
            a = self.haptics.address(role)
            pygame.draw.rect(self.screen, GRID, (bx, y + 6, 90, 10), 1)
            pygame.draw.rect(self.screen, MONSTER_COL, (bx, y + 6, int(90 * lvl), 10))
            self.text(f"{a or '-'} {role}", (bx, y + 18), MUTED)
            bx += 100
        self.setup_btn = pygame.Rect(bx, y + 4, 92, 30)
        pygame.draw.rect(self.screen, GRID, self.setup_btn, border_radius=6)
        self.text("DOT SETUP", (self.setup_btn.x + 10, self.setup_btn.y + 7), INK)
        y += 42
        if self.last_slash:
            s = self.last_slash
            age = time.monotonic() - s.t
            if s.hand == "head":
                what = f"head swing {s.direction}, {s.speed:.0f}°/s"
            else:
                what = f"{s.direction} ({s.hand} hand{'s' if s.hand == 'both' else ''}), {s.speed:.1f} sw/s"
            y = self.text(f"Last slash   {what}, {age:.1f}s ago", (x, y), BODY_COL if age < 1 else MUTED)
        y = H - 84
        for line in ("swing your head or arm (or SPACE) to slash   C calibrate",
                     "P dot setup   [ ] haptic strength   TAB camera   M music   R restart   ESC quit",
                     "no camera: ←/→ turn body   no AirPods: A/D turn head"):
            y = self.text(line, (x, y), MUTED) + 4

    def draw_hud(self):
        self.text(f"SCORE {self.score}", (20, 16), INK, self.big)
        self.text("♥" * self.lives + "·" * (LIVES - self.lives), (20, 52), MONSTER_COL, self.big)
        self.text(f"WAVE {self.wave}", (620, 16), MUTED, self.big)
        msg, t = self.banner
        if msg and time.monotonic() - t < 2.0 and self.state == "playing":
            self.text(msg, (RADAR_C[0], 110), WARN_COL, self.big, center=True)
        if self.state == "ready":
            self.text("BLIND SAMURAI", (RADAR_C[0], RADAR_C[1] - 60), INK, self.huge, center=True)
            self.text("Face the camera, press C to calibrate. Aim the sword with your head.",
                      (RADAR_C[0], RADAR_C[1] + 50), INK, center=True)
            self.text('then say "start game" (or slash) to begin.', (RADAR_C[0], RADAR_C[1] + 72), INK, center=True)
        elif self.state == "over":
            self.text("FALLEN", (RADAR_C[0], RADAR_C[1] - 60), MONSTER_COL, self.huge, center=True)
            self.text(f'score {self.score}, wave {self.wave}. Say "start game" to rise again.',
                      (RADAR_C[0], RADAR_C[1] + 60), INK, center=True)

    def draw(self):
        self.screen.fill(BG)
        self.draw_radar()
        self.draw_hud()
        self.draw_panel()
        if self.setup.open:
            self.setup.draw()
        pygame.display.flip()

    # ---------- main loop ----------
    def handle_key(self, key):
        if key == pygame.K_SPACE:
            self.slash(None)
        elif key == pygame.K_c:
            if self.pose:
                self.pose.calibrate()
            self.head.calibrate()
            self.sim_body = self.sim_head = 0.0
        elif key == pygame.K_TAB and self.pose:
            n = max(len(self.cameras), 2)
            self.pose.switch_camera((self.pose.camera + 1) % n)
        elif key == pygame.K_m:
            self.audio.music_on = not self.audio.music_on
        elif key in (pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET):
            step = 0.1 if key == pygame.K_RIGHTBRACKET else -0.1
            self.haptics.intensity = round(max(0.0, min(1.0, self.haptics.intensity + step)), 2)
        elif key == pygame.K_p:
            self.setup.open = True
        elif key == pygame.K_r:
            self.reset()
            self.state = "ready"

    def run(self):
        try:
            while True:
                dt = self.clock.tick(60) / 1000.0
                for e in pygame.event.get():
                    if e.type == pygame.QUIT:
                        return
                    if self.setup.open:
                        self.setup.handle(e)
                    elif e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q):
                        return
                    elif e.type == pygame.KEYDOWN:
                        self.handle_key(e.key)
                    elif e.type == pygame.MOUSEBUTTONDOWN and self.setup_btn.collidepoint(e.pos):
                        self.setup.open = True
                self.update(dt)
                self.draw()
        finally:
            self.shutdown()

    def shutdown(self):
        self.haptics.set_targets({})
        self.haptics.stop()
        self.haptics.join(timeout=3)   # it switches every dot off on its way out
        if self.pose:
            self.pose.stop()
        self.audio.close()
        pygame.quit()


def main():
    p = argparse.ArgumentParser(description="Blind Samurai")
    p.add_argument("--camera", type=int, help="OpenCV camera index (default: the laptop's built-in camera)")
    p.add_argument("--list-cameras", action="store_true")
    p.add_argument("--sound-test", action="store_true", help="open the sound test board instead of the game")
    p.add_argument("--no-camera", action="store_true", help="play with arrow keys instead")
    p.add_argument("--no-haptics", action="store_true")
    p.add_argument("--haptic-intensity", type=float, default=float(os.environ.get("BS_HAPTIC_INTENSITY", "1.0")),
                   help="overall haptic strength 0..1 (default 1, or BS_HAPTIC_INTENSITY); [ and ] adjust in game")
    p.add_argument("--no-airpods", action="store_true")
    args = p.parse_args()
    if args.sound_test:
        from .soundtest import main as sound_test
        return sound_test()
    if args.list_cameras:
        for i, name in enumerate(list_cameras()):
            print(f"{i}: {name}")
        return
    Game(args).run()

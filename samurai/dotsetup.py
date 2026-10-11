"""Dot setup screen: choose which physical DataFeel dot sits on the left side, chest centre,
right side and sword wrist, buzz dots to find them, and set the overall strength."""
import pygame

from .haptics import DEFAULT_ASSIGNMENT, MAX_ADDRESS, ROLES, Haptics

INK = (230, 230, 225)
MUTED = (130, 130, 125)
PANEL = (24, 25, 31)
LINE = (55, 57, 66)
ACCENT = (255, 150, 40)
OK_COL = (80, 210, 120)
OFF_COL = (90, 90, 95)

ROLE_TEXT = {"left": "LEFT SIDE", "center": "CHEST CENTER", "right": "RIGHT SIDE", "wrist": "SWORD WRIST"}


class DotSetup:
    def __init__(self, screen: pygame.Surface, haptics: Haptics, font, big):
        self.screen = screen
        self.haptics = haptics
        self.font = font
        self.big = big
        self.slot_font = pygame.font.SysFont("Menlo", 34, bold=True)
        self.open = False
        self.dragging = False

        # Figure seen from behind, so its left is your left.
        cx, top = 330, 190
        self.figure = (cx, top)
        self.slots = {
            "left": (cx - 105, top + 175),
            "center": (cx, top + 150),
            "right": (cx + 105, top + 175),
            "wrist": (cx + 205, top + 330),
        }
        self.slot_r = 34
        self.test_buttons = {r: pygame.Rect(x - 34, y + 62, 68, 26) for r, (x, y) in self.slots.items()}
        self.dot_rows = {}
        self.slider = pygame.Rect(760, 560, 420, 14)
        self.reset_btn = pygame.Rect(760, 640, 190, 40)
        self.done_btn = pygame.Rect(990, 640, 190, 40)

    def addresses(self) -> list[int]:
        found = set(self.haptics.connected)
        assigned = {a for a in self.haptics.assignment.values() if a}
        return sorted(found | assigned | set(range(1, MAX_ADDRESS + 1)))

    # ---------- input ----------
    def handle(self, e) -> None:
        if e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_p):
            self.open = False
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button in (1, 3):
            self._click(e.pos, forward=e.button == 1)
        elif e.type == pygame.MOUSEBUTTONUP:
            self.dragging = False
        elif e.type == pygame.MOUSEMOTION and self.dragging:
            self._set_strength(e.pos[0])

    def _click(self, pos, forward):
        for role, (x, y) in self.slots.items():
            if (pos[0] - x) ** 2 + (pos[1] - y) ** 2 <= (self.slot_r + 6) ** 2:
                self._cycle(role, 1 if forward else -1)
                return
        for role, rect in self.test_buttons.items():
            if rect.collidepoint(pos):
                a = self.haptics.address(role)
                if a:
                    self.haptics.test_address(a)
                return
        for a, (buzz_rect, role_rects) in self.dot_rows.items():
            if buzz_rect.collidepoint(pos):
                self.haptics.test_address(a)
                return
            for role, rect in role_rects.items():
                if rect.collidepoint(pos):
                    self.haptics.assign(role, a)
                    return
        if self.slider.inflate(0, 24).collidepoint(pos):
            self.dragging = True
            self._set_strength(pos[0])
        elif self.reset_btn.collidepoint(pos):
            for role in ROLES:
                self.haptics.assign(role, DEFAULT_ASSIGNMENT[role])
        elif self.done_btn.collidepoint(pos):
            self.open = False

    def _cycle(self, role, step):
        options = [None] + self.addresses()
        i = options.index(self.haptics.address(role)) if self.haptics.address(role) in options else 0
        self.haptics.assign(role, options[(i + step) % len(options)])

    def _set_strength(self, x):
        frac = (x - self.slider.x) / self.slider.w
        self.haptics.intensity = round(max(0.0, min(1.0, frac)), 2)

    # ---------- drawing ----------
    def text(self, s, pos, col=INK, font=None, center=False):
        surf = (font or self.font).render(s, True, col)
        r = surf.get_rect(center=pos) if center else surf.get_rect(topleft=pos)
        self.screen.blit(surf, r)
        return r

    def button(self, rect, label, hot=False):
        pygame.draw.rect(self.screen, ACCENT if hot else LINE, rect, border_radius=6)
        self.text(label, rect.center, (20, 20, 20) if hot else INK, center=True)

    def draw(self):
        w, h = self.screen.get_size()
        shade = pygame.Surface((w, h), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 200))
        self.screen.blit(shade, (0, 0))
        pygame.draw.rect(self.screen, PANEL, (40, 30, w - 80, h - 60), border_radius=12)
        self.text("DOT SETUP", (70, 55), INK, self.big)
        self.text("Click a spot to change its dot (right-click goes back). TEST buzzes it so you can feel where it is.",
                  (70, 100), MUTED)
        self.text("Figure seen from behind, so its left is your left.", (70, 120), MUTED)
        self._draw_figure()
        self._draw_dot_list()
        self._draw_strength()
        self.button(self.reset_btn, "Reset to default")
        self.button(self.done_btn, "Done  (P / Esc)", hot=True)

    def _draw_figure(self):
        cx, top = self.figure
        body = (70, 72, 82)
        pygame.draw.circle(self.screen, body, (cx, top + 30), 38)                         # head
        pygame.draw.rect(self.screen, body, (cx - 95, top + 80, 190, 230), border_radius=30)  # torso
        pygame.draw.line(self.screen, body, (cx + 92, top + 100), (cx + 175, top + 220), 26)  # right arm
        pygame.draw.line(self.screen, body, (cx + 175, top + 220), (cx + 205, top + 330), 22)
        pygame.draw.line(self.screen, body, (cx - 92, top + 100), (cx - 150, top + 300), 26)  # left arm
        # The sword in the right hand.
        pygame.draw.line(self.screen, (190, 190, 200), (cx + 210, top + 345), (cx + 300, top + 120), 5)

        mouse = pygame.mouse.get_pos()
        for role, (x, y) in self.slots.items():
            a = self.haptics.address(role)
            found = a in self.haptics.connected
            hover = (mouse[0] - x) ** 2 + (mouse[1] - y) ** 2 <= (self.slot_r + 6) ** 2
            lvl = self.haptics.target.get(role, 0.0)
            fill = (int(40 + 200 * lvl), 40, 45) if lvl > 0 else (35, 36, 42)
            pygame.draw.circle(self.screen, fill, (x, y), self.slot_r)
            ring = ACCENT if hover else (OK_COL if found else OFF_COL)
            pygame.draw.circle(self.screen, ring, (x, y), self.slot_r, 4)
            self.text(str(a) if a else "–", (x, y), INK if a else MUTED, self.slot_font, center=True)
            self.text(ROLE_TEXT[role], (x, y + self.slot_r + 14), INK, center=True)
            self.button(self.test_buttons[role], "TEST")

    def _draw_dot_list(self):
        x, y = 760, 150
        self.text("DOTS", (x, y), MUTED)
        self.text(self.haptics.status, (x + 60, y), OK_COL if self.haptics.connected else ACCENT)
        y += 30
        owner = {a: r for r, a in self.haptics.assignment.items() if a}
        self.dot_rows = {}
        for a in self.addresses():
            found = a in self.haptics.connected
            pygame.draw.circle(self.screen, OK_COL if found else OFF_COL, (x + 10, y + 16), 7)
            self.text(f"Dot {a}", (x + 26, y + 6), INK if found else MUTED)
            buzz = pygame.Rect(x + 100, y + 2, 64, 28)
            self.button(buzz, "BUZZ")
            role_rects = {}
            rx = x + 176
            for role in ROLES:
                r = pygame.Rect(rx, y + 2, 64, 28)
                self.button(r, role[:5].upper() if role != "center" else "CENTR", hot=owner.get(a) == role)
                role_rects[role] = r
                rx += 70
            self.dot_rows[a] = (buzz, role_rects)
            y += 40
        self.text("Click a role next to a dot to put that dot there.", (x, y + 6), MUTED)

    def _draw_strength(self):
        s = self.slider
        self.text(f"STRENGTH  {self.haptics.intensity:.0%}", (s.x, s.y - 30), INK)
        pygame.draw.rect(self.screen, LINE, s, border_radius=7)
        pygame.draw.rect(self.screen, ACCENT, (s.x, s.y, int(s.w * self.haptics.intensity), s.h), border_radius=7)
        pygame.draw.circle(self.screen, INK, (s.x + int(s.w * self.haptics.intensity), s.centery), 11)

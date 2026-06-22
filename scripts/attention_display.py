#!/usr/bin/env python3
"""
ATTENTION_DISPLAY_V1 — kontextová „pozornost" pro DRUHÝ 160×160 displej.

Display 1 (Eye_sphere CS-A) = Hansova tvář (avatar). Display 2 (CS-B) = TENTO
renderer = na co se Hans právě soustředí (jeho „mysl"). Stavový automat
(priorita shora): proaktivní podnět → Koláč dialog → vidí osobu → aktivita →
idle (nálada). Barva pozadí = aktuální nálada → tvář + pozornost = jeden organismus.

FÁZE A (teď): čistý software — `AttentionRenderer.render(ctx) -> PIL.Image (160×160)`.
Testovatelné bez hardwaru (`python3 -m scripts.attention_display` → PNG náhledy).
FÁZE B (až bude HW): frame → RGB565 → Eye_sphere GC9A01.send_frame() na CS-B.

ctx (dict):
  mood: str ('content'|'engaged'|'lonely'|'worried'|'curious'|'melancholic'|...)
  proactive: str|None      # text proaktivního podnětu (nejvyšší priorita)
  kolac: bool              # běží dialog s Koláčem
  kolac_speaking: bool
  person: str|None         # právě sledovaná rozpoznaná osoba
  person_min: int          # jak dlouho je přítomna (min)
  activity: str|None       # 'reading'|'watching'|'looking'
  activity_label: str|None # název (kniha/film)
  clock: str               # 'HH:MM'
  phase: str               # 'ráno'|'dopoledne'|'odpoledne'|'večer'|'noc'
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W = H = 160
R = 80
CX = CY = 80

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# Nálada → barva (RGB). Sjednocující ambient napříč stavy.
MOOD_COLORS = {
    "content":     (110, 200, 170),
    "engaged":     (90, 170, 255),
    "curious":     (180, 130, 245),
    "lonely":      (95, 120, 190),
    "worried":     (245, 175, 90),
    "melancholic": (130, 140, 180),
    "tired":       (140, 130, 160),
    "playful":     (255, 160, 120),
}
_DEFAULT_COLOR = (150, 160, 180)
_ACCENT = (255, 120, 150)            # proaktivní podnět (výrazný)
_BG = (12, 12, 20)


def _mood_color(mood: Optional[str]):
    return MOOD_COLORS.get((mood or "").lower(), _DEFAULT_COLOR)


class AttentionRenderer:
    def __init__(self, asset_dir: str = "data/avatar"):
        self.asset_dir = asset_dir
        self._fonts: dict = {}
        self._kolac = None

    # ── fonty ────────────────────────────────────────────────────────────────
    def _font(self, size: int, bold: bool = False):
        key = (size, bold)
        if key not in self._fonts:
            name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            try:
                self._fonts[key] = ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
            except Exception:
                self._fonts[key] = ImageFont.load_default()
        return self._fonts[key]

    def _text(self, d, cx, y, txt, size, fill=(235, 238, 248), bold=False, anchor="mm"):
        d.text((cx, y), txt, font=self._font(size, bold), fill=fill, anchor=anchor)

    # ── pozadí: tmavé + radiální záře v barvě nálady ──────────────────────────
    def _radial_bg(self, color, strength: float = 0.55):
        yy, xx = np.mgrid[0:H, 0:W]
        dist = np.sqrt((xx - CX) ** 2 + (yy - CY) ** 2) / R
        glow = np.clip(1.0 - dist, 0.0, 1.0) ** 1.6 * strength
        bg = np.array(_BG, dtype=np.float32)
        col = np.array(color, dtype=np.float32)
        img = bg[None, None, :] + (col - bg)[None, None, :] * glow[..., None]
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")

    def _round_mask(self, img):
        """Začerni rohy mimo vepsaný kruh (kulatý displej)."""
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, W - 1, H - 1], fill=255)
        out = Image.new("RGB", (W, H), (0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    def _kolac_img(self):
        if self._kolac is None:
            try:
                im = Image.open(os.path.join(self.asset_dir, "kolac.png")).convert("RGB")
                self._kolac = im.resize((104, 104))
            except Exception:
                self._kolac = False
        return self._kolac or None

    # ── stavy ────────────────────────────────────────────────────────────────
    def _render_mood(self, ctx):
        color = _mood_color(ctx.get("mood"))
        img = self._radial_bg(color, 0.6)
        d = ImageDraw.Draw(img)
        # jemný prstenec
        d.ellipse([CX - 58, CY - 58, CX + 58, CY + 58], outline=color, width=2)
        word = (ctx.get("mood") or "—").capitalize()
        self._text(d, CX, CY - 6, word, 26, bold=True)
        clk = ctx.get("clock") or ""
        phase = ctx.get("phase") or ""
        if clk:
            self._text(d, CX, CY + 30, clk, 16, fill=(210, 216, 232))
        if phase:
            self._text(d, CX, CY + 50, phase, 12, fill=(160, 168, 190))
        return img

    def _render_person(self, ctx):
        color = _mood_color(ctx.get("mood"))
        img = self._radial_bg(color, 0.4)
        d = ImageDraw.Draw(img)
        # focus-ring
        d.ellipse([CX - 62, CY - 62, CX + 62, CY + 62], outline=color, width=3)
        d.ellipse([CX - 70, CY - 70, CX + 70, CY + 70], outline=(color[0]//2, color[1]//2, color[2]//2), width=1)
        self._text(d, CX, CY - 30, "vidím", 12, fill=(180, 188, 210))
        name = (ctx.get("person") or "?").capitalize()
        self._text(d, CX, CY + 2, name, 30, bold=True)
        mins = ctx.get("person_min")
        if mins is not None:
            self._text(d, CX, CY + 36, f"už {int(mins)} min", 13, fill=(180, 188, 210))
        return img

    def _render_activity(self, ctx):
        color = _mood_color(ctx.get("mood"))
        img = self._radial_bg(color, 0.45)
        d = ImageDraw.Draw(img)
        act = ctx.get("activity") or "looking"
        verb = {"reading": "čte", "watching": "kouká", "looking": "rozhlíží se"}.get(act, act)
        self._draw_activity_icon(d, act, color)
        lbl = ctx.get("activity_label")
        self._text(d, CX, 112, verb, 15, bold=True)
        if lbl:
            self._text(d, CX, 134, self._fit(lbl, 18), 12, fill=(190, 196, 214))
        return img

    def _draw_activity_icon(self, d, act, color):
        cy = 60
        if act == "reading":            # otevřená kniha
            d.polygon([(CX-34, cy+18), (CX, cy+8), (CX, cy-22), (CX-34, cy-12)], outline=color, width=2)
            d.polygon([(CX+34, cy+18), (CX, cy+8), (CX, cy-22), (CX+34, cy-12)], outline=color, width=2)
        elif act == "watching":         # obrazovka
            d.rounded_rectangle([CX-32, cy-22, CX+32, cy+16], radius=6, outline=color, width=3)
            d.line([CX-10, cy+24, CX+10, cy+24], fill=color, width=3)
        else:                            # oko (rozhlíží se)
            d.ellipse([CX-34, cy-16, CX+34, cy+16], outline=color, width=2)
            d.ellipse([CX-9, cy-9, CX+9, cy+9], fill=color)

    def _render_kolac(self, ctx):
        img = self._radial_bg((230, 200, 120), 0.35)
        d = ImageDraw.Draw(img)
        k = self._kolac_img()
        if k:
            img.paste(k, (CX - 52, 22))
        else:
            d.ellipse([CX-40, 22, CX+40, 102], fill=(210, 180, 110))
        # ATTENTION_CYCLE_V1 — žlutý kroužek odstraněn
        speaking = ctx.get("kolac_speaking")
        self._text(d, CX, 130, "Koláč" + (" mluví" if speaking else ""), 16, bold=True)
        return img

    def _render_proactive(self, ctx):
        img = self._radial_bg(_ACCENT, 0.6)
        d = ImageDraw.Draw(img)
        d.ellipse([CX - 60, CY - 60, CX + 60, CY + 60], outline=_ACCENT, width=4)
        # bublina
        d.rounded_rectangle([CX-40, CY-44, CX+40, CY-6], radius=8, outline=(255, 235, 240), width=3)
        d.polygon([(CX-12, CY-6), (CX-2, CY-6), (CX-14, CY+8)], fill=(255, 235, 240))
        txt = self._fit(ctx.get("proactive") or "Dotaz pro Vás", 18)
        self._text(d, CX, CY + 34, txt, 13, fill=(255, 240, 245), bold=True)
        return img

    @staticmethod
    def _fit(s: str, n: int) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    # ATTENTION_CYCLE_V1 — hodiny
    def _render_clock(self, ctx):
        color = _mood_color(ctx.get("mood"))
        img = self._radial_bg(color, 0.5)
        d = ImageDraw.Draw(img)
        d.ellipse([CX - 60, CY - 60, CX + 60, CY + 60], outline=color, width=2)
        clk = ctx.get("clock") or ""
        self._text(d, CX, CY - 4, clk, 40, bold=True)
        phase = ctx.get("phase") or ""
        if phase:
            self._text(d, CX, CY + 36, phase, 14, fill=(190, 196, 214))
        return img

    # ATTENTION_CYCLE_V1 — teplota procesoru
    def _render_cpu(self, ctx):
        t = ctx.get("cpu_temp")
        tv = float(t) if t is not None else 0.0
        if tv < 60:
            color = (110, 200, 170)
        elif tv < 75:
            color = (245, 200, 90)
        else:
            color = (245, 110, 90)
        img = self._radial_bg(color, 0.5)
        d = ImageDraw.Draw(img)
        d.ellipse([CX - 58, CY - 58, CX + 58, CY + 58], outline=color, width=2)
        self._text(d, CX, CY - 28, "CPU", 14, fill=(190, 196, 214))
        txt = f"{tv:.0f}°C" if t is not None else "—"
        self._text(d, CX, CY + 4, txt, 36, bold=True)
        return img

    # ── cyklus karet (ATTENTION_CYCLE_V1) ──────────────────────────────────
    def cycle_cards(self, ctx) -> list:
        """Karty k rotaci (jen ty, co dávají v ctx smysl). Override (proactive/
        kolac) řeší override_card a do cyklu nepatří."""
        cards = []
        if ctx.get("person"):
            cards.append("person")
        if ctx.get("activity"):
            cards.append("activity")
        cards.append("mood")
        cards.append("clock")
        if ctx.get("cpu_temp") is not None:
            cards.append("cpu")
        return cards

    def override_card(self, ctx):
        """Přeruší cyklus — ukáže se okamžitě po dobu trvání."""
        if ctx.get("proactive"):
            return "proactive"
        if ctx.get("kolac"):
            return "kolac"
        return None

    def render_card(self, ctx, card) -> Image.Image:
        fn = {
            "proactive": self._render_proactive,
            "kolac": self._render_kolac,
            "person": self._render_person,
            "activity": self._render_activity,
            "mood": self._render_mood,
            "clock": self._render_clock,
            "cpu": self._render_cpu,
        }.get(card, self._render_mood)
        return self._round_mask(fn(ctx))

    # ── priorita + render ─────────────────────────────────────────────────────
    def resolve(self, ctx) -> str:
        if ctx.get("proactive"):
            return "proactive"
        if ctx.get("kolac"):
            return "kolac"
        if ctx.get("person"):
            return "person"
        if ctx.get("activity"):
            return "activity"
        return "mood"

    def render(self, ctx) -> Image.Image:
        kind = self.resolve(ctx)
        fn = {
            "proactive": self._render_proactive,
            "kolac": self._render_kolac,
            "person": self._render_person,
            "activity": self._render_activity,
            "mood": self._render_mood,
        }[kind]
        return self._round_mask(fn(ctx))


# ── náhledy (Fáze A — bez hardwaru) ──────────────────────────────────────────
if __name__ == "__main__":
    out_dir = "data/avatar/attention_preview"
    os.makedirs(out_dir, exist_ok=True)
    r = AttentionRenderer()
    samples = {
        "1_mood_idle":   {"mood": "content", "clock": "14:27", "phase": "odpoledne"},
        "2_person":      {"mood": "engaged", "person": "alice", "person_min": 3},
        "3_activity_read": {"mood": "curious", "activity": "reading", "activity_label": "Ivanhoe"},
        "4_activity_watch": {"mood": "engaged", "activity": "watching", "activity_label": "Kde vládne ticho"},
        "5_kolac":       {"mood": "playful", "kolac": True, "kolac_speaking": True},
        "6_proactive":   {"mood": "engaged", "proactive": "Jak dopadla ta zkouška?"},
        "7_mood_worried": {"mood": "worried", "clock": "23:10", "phase": "noc"},
    }
    for name, ctx in samples.items():
        img = r.render(ctx)
        path = os.path.join(out_dir, f"{name}.png")
        img.save(path)
        print(f"  {name:18s} → {r.resolve(ctx):9s} {path}")
    print(f"Náhledy v {out_dir}/")

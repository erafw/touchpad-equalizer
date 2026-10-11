"""
TouchPad Equalizer for Windows — v3.1.0
by erafw
"""

import threading
import time
import ctypes
import ctypes.wintypes
import math
import json
import os
import sys
import tkinter as tk
from tkinter import ttk
from dataclasses import dataclass, asdict
from typing import Optional

import keyboard
import pystray
from PIL import Image, ImageDraw

# ═══════════════════════════════════════════════════════════════════════════════
# Win32
# ═══════════════════════════════════════════════════════════════════════════════

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("_input", INPUT_UNION)]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          ctypes.wintypes.POINT),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("flags",       ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG)),
    ]

MOUSEEVENTF_MOVE = 0x0001
INPUT_MOUSE      = 0
WH_MOUSE_LL      = 14
WM_MOUSEMOVE     = 0x0200
HC_ACTION        = 0
INJECTED_FLAG    = 0x00000001

user32   = ctypes.WinDLL("user32",   use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

user32.SetWindowsHookExW.restype  = ctypes.wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                      ctypes.wintypes.HINSTANCE,
                                      ctypes.wintypes.DWORD]
user32.CallNextHookEx.restype  = ctypes.c_long
user32.CallNextHookEx.argtypes = [ctypes.wintypes.HHOOK, ctypes.c_int,
                                   ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]

def send_correction(dx: int, dy: int):
    if dx == 0 and dy == 0:
        return
    inp = INPUT(
        type=INPUT_MOUSE,
        _input=INPUT_UNION(mi=MOUSEINPUT(
            dx=dx, dy=dy, mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE,
            time=0, dwExtraInfo=None
        ))
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".touchpad_eq_v3.json")

@dataclass
class Config:
    smoothing:       float = 0.45
    jitter_radius:   float = 1.5
    sensitivity:     float = 1.0
    accel_exponent:  float = 1.4
    accel_threshold: float = 8.0
    accel_max_boost: float = 3.0
    toggle_hotkey:   str   = "ctrl+alt+t"
    enabled:         bool  = True

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls):
        if not os.path.exists(CONFIG_FILE):
            return cls()
        try:
            with open(CONFIG_FILE) as f:
                d = json.load(f)
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except Exception:
            return cls()

# ═══════════════════════════════════════════════════════════════════════════════
# Filtros
# ═══════════════════════════════════════════════════════════════════════════════

class EMAFilter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sx = self.sy = 0.0

    def reset(self):
        self.sx = self.sy = 0.0

    def process(self, dx: float, dy: float):
        speed = math.hypot(dx, dy)
        if speed < self.cfg.jitter_radius:
            self.sx *= 0.2
            self.sy *= 0.2
            return -dx, -dy

        norm  = min(speed / 25.0, 1.0)
        alpha = self.cfg.smoothing + norm * (1.0 - self.cfg.smoothing)
        self.sx = alpha * dx + (1 - alpha) * self.sx
        self.sy = alpha * dy + (1 - alpha) * self.sy

        out_speed = math.hypot(self.sx, self.sy)
        scaled    = out_speed * self.cfg.sensitivity
        if scaled > self.cfg.accel_threshold:
            excess = scaled - self.cfg.accel_threshold
            boost  = 1.0 + (excess / self.cfg.accel_threshold) ** self.cfg.accel_exponent
            boost  = min(boost, self.cfg.accel_max_boost)
            scaled *= boost

        ratio      = (scaled / out_speed) if out_speed > 1e-6 else 1.0
        filtered_x = self.sx * ratio
        filtered_y = self.sy * ratio
        return filtered_x - dx, filtered_y - dy

# ═══════════════════════════════════════════════════════════════════════════════
# Equalizer
# ═══════════════════════════════════════════════════════════════════════════════

class TouchpadEqualizer:
    def __init__(self, cfg: Config):
        self.cfg      = cfg
        self.ema      = EMAFilter(cfg)
        self._lock    = threading.Lock()
        self._accum_x = 0.0
        self._accum_y = 0.0
        self._hook_id   = None
        self._hook_proc = None
        self._last_x: Optional[int] = None
        self._last_y: Optional[int] = None

    def _hook_fn(self, nCode, wParam, lParam):
        try:
            if nCode == HC_ACTION and wParam == WM_MOUSEMOVE:
                ms = ctypes.cast(
                    ctypes.c_void_p(lParam),
                    ctypes.POINTER(MSLLHOOKSTRUCT)
                ).contents

                if ms.flags & INJECTED_FLAG:
                    return user32.CallNextHookEx(self._hook_id, nCode, wParam, lParam)

                if self.cfg.enabled and self._last_x is not None:
                    dx = float(ms.pt.x - self._last_x)
                    dy = float(ms.pt.y - self._last_y)
                    if dx != 0 or dy != 0:
                        with self._lock:
                            corr_x, corr_y = self.ema.process(dx, dy)
                            self._accum_x += corr_x
                            self._accum_y += corr_y
                            ix = int(self._accum_x)
                            iy = int(self._accum_y)
                            self._accum_x -= ix
                            self._accum_y -= iy
                        send_correction(ix, iy)

                self._last_x = ms.pt.x
                self._last_y = ms.pt.y

        except Exception as e:
            print(f"[EQ] erro: {e}")

        return user32.CallNextHookEx(self._hook_id, nCode, wParam, lParam)

    def _run_hook(self):
        self._hook_proc = HOOKPROC(self._hook_fn)
        self._hook_id   = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._hook_proc, None, 0
        )
        if not self._hook_id:
            print("[EQ] ERRO ao instalar hook.")
            return
        print("[EQ] Hook ativo.")
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook_id)

    def start(self):
        keyboard.add_hotkey(self.cfg.toggle_hotkey, self._toggle)
        t = threading.Thread(target=self._run_hook, daemon=True, name="EQ-Hook")
        t.start()
        time.sleep(0.2)
        handle = kernel32.OpenThread(0x0020, False, t.ident)
        if handle:
            kernel32.SetThreadPriority(handle, 15)
            kernel32.CloseHandle(handle)

    def _toggle(self):
        self.cfg.enabled = not self.cfg.enabled
        self.ema.reset()
        self._last_x = self._last_y = None
        print(f"[EQ] {'ATIVO' if self.cfg.enabled else 'DESATIVADO'}")

    def update_config(self, cfg: Config):
        with self._lock:
            self.cfg     = cfg
            self.ema.cfg = cfg
            self.ema.reset()
            self._last_x = self._last_y = None

# ═══════════════════════════════════════════════════════════════════════════════
# Painel — tkinter com visual escuro estilo Apple
# ═══════════════════════════════════════════════════════════════════════════════

BG       = "#1c1c1e"
BG_CARD  = "#2c2c2e"
BG_BTN   = "#3a3a3c"
BLUE     = "#0A84FF"
GREEN    = "#34C759"
RED      = "#FF453A"
FG       = "#ffffff"
FG_DIM   = "#888888"
FONT     = ("Helvetica", 13)
FONT_SM  = ("Helvetica", 10)
FONT_HDR = ("Helvetica", 18, "bold")
FONT_SEC = ("Helvetica", 11, "bold")
RADIUS   = 10

class ConfigPanel:
    def __init__(self, cfg: Config, eq: TouchpadEqualizer):
        self.cfg = cfg
        self.eq  = eq
        self.win = None

    def show(self):
        if self.win:
            try:
                if self.win.winfo_exists():
                    self.win.deiconify()
                    self.win.lift()
                    self.win.focus_force()
                    return
            except Exception:
                pass

        self.win = tk.Tk()
        self.win.title("TouchPad EQ  v3.1.0  —  by erafw")
        self.win.geometry("440x580")
        self.win.resizable(False, False)
        self.win.configure(bg=BG)
        self.win.attributes('-topmost', True)
        self.win.after(300, lambda: self.win.attributes('-topmost', False))

        self._build_ui()
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        self.win.mainloop()

    def _card(self, parent):
        f = tk.Frame(parent, bg=BG_CARD, bd=0)
        f.pack(fill="x", padx=20, pady=6)
        return f

    def _build_ui(self):
        cfg = self.cfg
        win = self.win

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(win, bg=BG)
        hdr.pack(fill="x", padx=24, pady=(24, 2))

        tk.Label(hdr, text="🎛️  TouchPad Equalizer",
                 font=FONT_HDR, bg=BG, fg=FG).pack(side="left")

        self.v_enabled = tk.BooleanVar(value=cfg.enabled)
        tk.Checkbutton(
            hdr, text="Ativo", variable=self.v_enabled,
            command=self._apply,
            bg=BG, fg=GREEN, selectcolor=BG,
            activebackground=BG, activeforeground=GREEN,
            font=FONT, cursor="hand2",
        ).pack(side="right", padx=4)

        tk.Label(win, text="by erafw", font=FONT_SM,
                 bg=BG, fg=FG_DIM).pack(anchor="w", padx=28, pady=(0, 8))

        tk.Frame(win, bg="#3a3a3c", height=1).pack(fill="x", padx=20, pady=4)

        # ── Helper slider ─────────────────────────────────────────────────────
        def section(icon, title):
            outer = tk.Frame(win, bg=BG_CARD, bd=0)
            outer.pack(fill="x", padx=20, pady=6)
            tk.Label(outer, text=f"  {icon}  {title}",
                     font=FONT_SEC, bg=BG_CARD, fg=FG_DIM
                     ).pack(anchor="w", padx=8, pady=(10, 2))
            return outer

        def slider_row(parent, label, var, lo, hi):
            row = tk.Frame(parent, bg=BG_CARD)
            row.pack(fill="x", padx=12, pady=(2, 8))

            top = tk.Frame(row, bg=BG_CARD)
            top.pack(fill="x")
            tk.Label(top, text=label, font=FONT,
                     bg=BG_CARD, fg=FG).pack(side="left")
            val_lbl = tk.Label(top, text=f"{var.get():.2f}",
                               font=FONT, bg=BG_CARD, fg=FG_DIM)
            val_lbl.pack(side="right")

            def upd(v):
                val_lbl.config(text=f"{float(v):.2f}")
                self._apply()

            tk.Scale(
                row, from_=lo, to=hi, resolution=0.01,
                orient="horizontal", variable=var, command=upd,
                showvalue=False, bg=BG_CARD, fg=FG,
                troughcolor="#3a3a3c", activebackground=BLUE,
                highlightthickness=0, bd=0, sliderrelief="flat",
                sliderlength=20,
            ).pack(fill="x", pady=(2, 0))

        # ── Variáveis ─────────────────────────────────────────────────────────
        self.v_smooth = tk.DoubleVar(value=cfg.smoothing)
        self.v_jitter = tk.DoubleVar(value=cfg.jitter_radius)
        self.v_sens   = tk.DoubleVar(value=cfg.sensitivity)
        self.v_exp    = tk.DoubleVar(value=cfg.accel_exponent)
        self.v_boost  = tk.DoubleVar(value=cfg.accel_max_boost)

        # ── Seções ────────────────────────────────────────────────────────────
        f1 = section("〰️", "Suavização")
        slider_row(f1, "Intensidade",      self.v_smooth, 0.05, 1.0)
        slider_row(f1, "Raio anti-jitter", self.v_jitter, 0.2,  4.0)

        f2 = section("🚀", "Velocidade")
        slider_row(f2, "Sensibilidade",   self.v_sens,  0.3, 3.0)
        slider_row(f2, "Curva de accel.", self.v_exp,   1.0, 3.0)
        slider_row(f2, "Boost máximo",    self.v_boost, 1.0, 5.0)

        # ── Botões ────────────────────────────────────────────────────────────
        bf = tk.Frame(win, bg=BG)
        bf.pack(fill="x", padx=20, pady=(10, 4))

        tk.Button(
            bf, text="💾  Salvar", command=self._save,
            bg=BLUE, fg=FG, font=FONT,
            relief="flat", cursor="hand2",
            activebackground="#0060CC", activeforeground=FG,
            padx=12, pady=8,
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        tk.Button(
            bf, text="↺  Resetar", command=self._reset,
            bg=BG_BTN, fg=RED, font=FONT,
            relief="flat", cursor="hand2",
            activebackground="#4a3a3c", activeforeground=RED,
            padx=12, pady=8,
        ).pack(side="left", expand=True, fill="x", padx=(6, 0))

        tk.Label(win, text=f"toggle: {cfg.toggle_hotkey}",
                 font=FONT_SM, bg=BG, fg="#3a3a3a").pack(pady=(8, 16))

    def _apply(self):
        self.cfg.smoothing       = self.v_smooth.get()
        self.cfg.jitter_radius   = self.v_jitter.get()
        self.cfg.sensitivity     = self.v_sens.get()
        self.cfg.accel_exponent  = self.v_exp.get()
        self.cfg.accel_max_boost = self.v_boost.get()
        self.cfg.enabled         = self.v_enabled.get()
        self.eq.update_config(self.cfg)

    def _save(self):
        self._apply()
        self.cfg.save()

    def _reset(self):
        self.cfg = Config()
        self.eq.update_config(self.cfg)
        self.win.destroy()
        self.win = None
        self.show()

# ═══════════════════════════════════════════════════════════════════════════════
# Tray
# ═══════════════════════════════════════════════════════════════════════════════

def make_icon():
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill="#0A84FF")
    draw.text((16, 20), "EQ", fill="white")
    return img

def run_tray(cfg: Config, eq: TouchpadEqualizer, panel: ConfigPanel):
    def on_config(icon, item):
        threading.Thread(target=panel.show, daemon=True).start()

    def on_toggle(icon, item):
        cfg.enabled = not cfg.enabled
        eq.update_config(cfg)

    def on_quit(icon, item):
        cfg.save()
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Configurações", on_config, default=True),
        pystray.MenuItem(
            lambda _: f"{'✓ Ativo' if cfg.enabled else '✗ Inativo'}",
            on_toggle
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair", on_quit),
    )
    pystray.Icon("TouchpadEQ", make_icon(), "TouchPad EQ v3.1.0 — by erafw", menu).run()

# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if not ctypes.windll.shell32.IsUserAnAdmin():
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{os.path.abspath(__file__)}"', None, 1
        )
        sys.exit(0)

    cfg   = Config.load()
    eq    = TouchpadEqualizer(cfg)
    panel = ConfigPanel(cfg, eq)

    eq.start()
    print(f"[EQ] Rodando. Toggle: {cfg.toggle_hotkey}")
    run_tray(cfg, eq, panel)

if __name__ == "__main__":
    main()
"""
TouchPad Equalizer for Windows — v3 (fix feedback loop)
Estratégia: não suprime o evento original.
Aplica apenas a DIFERENÇA entre o movimento bruto e o filtrado.
"""

import threading
import time
import ctypes
import ctypes.wintypes
import math
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import keyboard
import pystray
from PIL import Image, ImageDraw

try:
    import customtkinter as ctk
    CTK_AVAILABLE = True
except ImportError:
    CTK_AVAILABLE = False

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
    """Injeta apenas a correção (diferença entre filtrado e bruto)."""
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
    smoothing:       float = 0.45   # 0=máximo suave, 1=sem filtro
    jitter_radius:   float = 1.5    # px — zona morta anti-tremor
    sensitivity:     float = 1.0    # multiplicador base
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
    """Suaviza o movimento. Retorna quanto MOVER A MAIS ou A MENOS."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sx = self.sy = 0.0

    def reset(self):
        self.sx = self.sy = 0.0

    def process(self, dx: float, dy: float):
        speed = math.hypot(dx, dy)

        # Zona de jitter — suprime micro-tremores
        if speed < self.cfg.jitter_radius:
            self.sx *= 0.2
            self.sy *= 0.2
            # Retorna o negativo do bruto para cancelar o movimento original
            return -dx, -dy

        # Alpha adaptativo: movimento rápido = menos suavização
        norm  = min(speed / 25.0, 1.0)
        alpha = self.cfg.smoothing + norm * (1.0 - self.cfg.smoothing)

        self.sx = alpha * dx + (1 - alpha) * self.sx
        self.sy = alpha * dy + (1 - alpha) * self.sy

        # Ballistics
        out_speed = math.hypot(self.sx, self.sy)
        scaled    = out_speed * self.cfg.sensitivity
        if scaled > self.cfg.accel_threshold:
            excess = scaled - self.cfg.accel_threshold
            boost  = 1.0 + (excess / self.cfg.accel_threshold) ** self.cfg.accel_exponent
            boost  = min(boost, self.cfg.accel_max_boost)
            scaled *= boost

        ratio = (scaled / out_speed) if out_speed > 1e-6 else 1.0
        filtered_x = self.sx * ratio
        filtered_y = self.sy * ratio

        # Retorna apenas a DIFERENÇA (correção) em relação ao bruto
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

                # Ignora eventos injetados pelo próprio app
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

                        # Injeta só a correção — o movimento bruto já foi aplicado
                        send_correction(ix, iy)

                self._last_x = ms.pt.x
                self._last_y = ms.pt.y

        except Exception as e:
            print(f"[EQ] erro (passando adiante): {e}")

        # SEMPRE passa o evento original adiante — sem suprimir
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
# Painel
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigPanel:
    def __init__(self, cfg: Config, eq: TouchpadEqualizer):
        self.cfg = cfg
        self.eq  = eq
        self.win = None

    def show(self):
        if self.win:
            try:
                if self.win.winfo_exists():
                    self.win.lift()
                    return
            except Exception:
                pass

        import tkinter as tk
        self.win = tk.Tk()
        self.win.title("🎛️ TouchPad Equalizer v3 — by erafw")
        self.win.geometry("480x420")
        self.win.resizable(False, False)
        self.win.attributes('-topmost', True)
        self.win.after(200, lambda: self.win.attributes('-topmost', False))
        self.win.after(100, lambda: self.win.state('normal'))
        self.win.after(150, lambda: self.win.lift())
        self.win.after(200, lambda: self.win.focus_force())

        cfg = self.cfg

        def section(text):
            f = tk.LabelFrame(self.win, text=text,
                              font=("Segoe UI", 10, "bold"), padx=8, pady=6)
            f.pack(fill="x", padx=12, pady=6)
            return f

        def row(parent, label, var, lo, hi):
            r = tk.Frame(parent)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=label, width=22, anchor="w").pack(side="left")
            lv = tk.Label(r, text=f"{var.get():.2f}", width=6)
            lv.pack(side="right")
            def upd(v):
                lv.config(text=f"{float(v):.2f}")
                self._apply()
            tk.Scale(r, from_=lo, to=hi, resolution=0.01, orient="horizontal",
                     variable=var, command=upd, showvalue=False
                     ).pack(side="left", fill="x", expand=True)

        self.v_enabled  = tk.BooleanVar(value=cfg.enabled)
        self.v_smooth   = tk.DoubleVar(value=cfg.smoothing)
        self.v_jitter   = tk.DoubleVar(value=cfg.jitter_radius)
        self.v_sens     = tk.DoubleVar(value=cfg.sensitivity)
        self.v_exp      = tk.DoubleVar(value=cfg.accel_exponent)
        self.v_boost    = tk.DoubleVar(value=cfg.accel_max_boost)

        f0 = section("⚡ Status")
        tk.Checkbutton(f0, text="Equalizador ativo",
                       variable=self.v_enabled,
                       command=self._apply).pack(anchor="w")

        f1 = section("〰️ Suavização")
        row(f1, "Suavização (0=max, 1=off)", self.v_smooth, 0.05, 1.0)
        row(f1, "Raio anti-jitter (px)",     self.v_jitter, 0.2,  4.0)

        f2 = section("🚀 Velocidade")
        row(f2, "Sensibilidade",    self.v_sens,  0.3, 3.0)
        row(f2, "Curva de accel.", self.v_exp,   1.0, 3.0)
        row(f2, "Boost máximo",    self.v_boost, 1.0, 5.0)

        bf = tk.Frame(self.win)
        bf.pack(fill="x", padx=12, pady=10)
        tk.Button(bf, text="💾 Salvar",  command=self._save,  width=14).pack(side="left",  padx=4)
        tk.Button(bf, text="↺ Resetar", command=self._reset, width=14).pack(side="right", padx=4)

        tk.Label(self.win, text=f"Toggle: {cfg.toggle_hotkey}",
                 fg="gray", font=("Segoe UI", 8)).pack()

        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        self.win.after(100, lambda: self.win.lift())
        self.win.after(100, lambda: self.win.focus_force())
        self.win.mainloop()

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
    draw.ellipse([4, 4, 60, 60], fill="#1565C0")
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
    pystray.Icon("TouchpadEQ", make_icon(), "TouchPad EQ v3 — by erafw", menu).run()

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
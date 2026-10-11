<div align="center">

# 🎛️ TouchPad Equalizer

**Transform your bad touchpad into a premium experience.**  
A real-time input filter for Windows that eliminates jitter, smooths movement and adds intelligent acceleration — like a software upgrade for your hardware.

![Version](https://img.shields.io/badge/version-3.1.0-blue?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-yellow?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

</div>

---

## ✨ What it does

Most budget laptops ship with touchpads that feel rough — shaky cursor, imprecise clicks, no acceleration curve. TouchPad Equalizer hooks into Windows at the lowest level and applies real-time filters before the cursor moves, making even cheap touchpads feel smooth and responsive.

---

## 🚀 Features

- **Adaptive EMA Filter** — eliminates micro-jitter when your finger is still, while staying responsive during fast movements
- **Custom Ballistics Curve** — fine-grained control at low speeds, clean progressive acceleration at high speeds
- **Real-time controls** — adjust all parameters live with sliders, no restart needed
- **Global toggle hotkey** — `Ctrl + Alt + T` to instantly compare with/without the equalizer
- **System tray** — runs silently in the background, zero distraction
- **Dark UI** — clean, minimal interface inspired by macOS

---

## 📸 Screenshot

<img width="438" height="609" alt="Captura de tela 2026-10-11 132754" src="https://github.com/user-attachments/assets/3396bac4-2c04-4838-a654-f42883f5c03c" />


---

## 📦 Installation

### Option 1 — Download the .exe (easiest)

1. Go to the [`dist/`](./dist/) folder
2. Download `TouchpadEQ.exe`
3. Right-click → **Run as Administrator**
4. Look for the **EQ** icon in the system tray

### Option 2 — Run from source

**Requirements:** Python 3.10+

```bash
pip install pynput pystray Pillow customtkinter keyboard
python touchpad_eq.py
```

> Must be run as **Administrator** for the low-level hook to work.

---

## ⚙️ Configuration

Open the settings panel by **double-clicking the EQ tray icon**.

| Parameter | Description |
|---|---|
| **Smoothing** | Filter intensity — lower = smoother, higher = more responsive |
| **Jitter radius** | Dead zone in pixels — suppresses micro-tremors when finger is still |
| **Sensitivity** | Base speed multiplier |
| **Accel curve** | Exponent of the acceleration curve |
| **Max boost** | Maximum speed multiplier at high velocity |

Settings are saved automatically to `~/.touchpad_eq_v3.json`.

---

## ⌨️ Hotkeys

| Hotkey | Action |
|---|---|
| `Ctrl + Alt + T` | Toggle equalizer on/off |

----

<img width="681" height="590" alt="Ekran görüntüsü 2026-10-14 214510" src="https://github.com/user-attachments/assets/7f12c49b-d936-4d1e-b8f1-0c4513f3e710" />

# UHF-Media Video Studio Player

A desktop video player (Windows, PyQt5 + libmpv) with built‑in editing tools —
rotate, crop, trim, export — merged with an integrated **"Identify Song"**
feature powered by [Shazam](https://www.shazam.com/) (via
[`shazamio`](https://github.com/dotX12/ShazamIO)).

Click the 🎵 button while any video or audio file is open, and the app will
sample ~10 seconds of audio from the middle of the file, send it to Shazam,
and — if it gets a match — rename the file to `Artist - Title.ext` on disk.
If the track isn't recognized (or anything goes wrong), **the file is left
completely untouched**.

## Features

- Video playback via `libmpv` (subtitles, audio track switching, screenshots,
  fullscreen, keyboard/mouse shortcuts)
- Rotate (90° CW/CCW/180°), crop, and trim with `ffmpeg`-based export
  (hardware encoding: NVENC / QSV / AMF, with automatic CPU fallback)
- Single-instance mode: double‑clicking another video file re-uses the
  already‑open window instead of spawning a new one
- **Shazam song recognition + auto‑rename**, non‑destructive by design

## Requirements

| Component | Notes |
|---|---|
| Python 3.10 – 3.13 | Tested on 3.13 |
| [PyQt5](https://pypi.org/project/PyQt5/) | GUI framework |
| [python-mpv](https://pypi.org/project/mpv/) + `libmpv-2.dll` | Video engine — the DLL must sit next to the script/exe or be on `PATH` |
| `ffmpeg.exe` + `ffprobe.exe` | For export (rotate/crop/trim) and for extracting audio for Shazam. Must be next to the script/exe or on `PATH` |
| `numpy` | Audio buffer handling |
| `shazamio` (+ its dependencies) | Song recognition |
| Internet connection | Required at runtime for the Shazam API call |

## Installation

```bash
pip install PyQt5 mpv numpy
```

### Installing `shazamio` on Windows / Python 3.13

`shazamio` depends on a Rust‑compiled component, `shazamio-core`. As of this
writing, the exact version `shazamio` asks for (`1.1.2`) has **no prebuilt
Windows wheel for Python 3.13**, so a plain `pip install shazamio` tries to
compile it from source — which fails unless you have Rust *and* the MSVC
linker (Visual Studio Build Tools) installed.

The simplest fix is to install a newer `shazamio-core` that *does* ship a
prebuilt Windows wheel (it's forward‑compatible via the `abi3` ABI tag), then
install `shazamio` itself without letting pip downgrade it:

```bash
pip install shazamio-core==1.2.0
pip install shazamio==0.8.1 --no-deps
pip install aiofiles aiohttp aiohttp-retry anyio dataclass-factory pydantic pydub
```

pip will print a `shazamio 0.8.1 requires shazamio-core==1.1.2, but you have
1.2.0` warning — this is expected and harmless; it's not an error.

### Python 3.13: missing `audioop`

Python 3.13 removed the built-in `audioop` module, which `pydub` (a
`shazamio` dependency) still imports. Install the official backport:

```bash
pip install audioop-lts
```

### Verifying the install

```bash
python -c "import numpy, shazamio; from shazamio import Shazam; print('OK')"
```

If this prints `OK`, everything needed for the music‑ID feature is in place.

## Running

```bash
python video_studio_player.py
```

or double‑click a video file associated with the app; the path is picked up
from `sys.argv[1]`.

## Building a standalone .exe (PyInstaller)

```bash
pip install pyinstaller
python -m PyInstaller --onedir --windowed --name VideoStudioPlayer --icon=app.ico video_studio_player.py
```

- Omit `--icon=app.ico` if you don't have an icon file, or point it at your
  own `.ico`.
- If you re-run PyInstaller after changing dependencies, delete the stale
  `build/`, `dist/`, and `*.spec` first so the new imports are picked up:
  ```bash
  del VideoStudioPlayer.spec
  rmdir /s /q build
  rmdir /s /q dist
  ```

### Files PyInstaller does **not** bundle automatically

These are external binaries, not Python packages — copy them manually into
`dist\VideoStudioPlayer\` after building:

- `ffmpeg.exe`
- `ffprobe.exe`
- `libmpv-2.dll`

Without them, playback may still work depending on how libmpv is resolved,
but export and song‑recognition will fail.

### Distributing the app

With `--onedir`, ship the **entire** `dist\VideoStudioPlayer\` folder (the
`.exe`, the `_internal` folder, and the three files above) — not just the
`.exe` on its own.

On a very bare/clean Windows machine you may also need the
[Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist),
since `shazamio-core`'s compiled component depends on it. Most up-to-date
Windows 10/11 systems already have it.

## How song recognition works

1. Click the 🎵 button (next to the screenshot button in the bottom
   controls).
2. The app reads the current file's duration from `mpv`. If it's longer than
   30 seconds, it seeks to the midpoint; otherwise it reads from the start.
3. ~10 seconds of stereo PCM audio is extracted with `ffmpeg` and checked for
   silence (very quiet clips are skipped, not sent to Shazam).
4. The audio is sent to Shazam via `shazamio`, in a background thread so the
   UI never freezes.
5. **Match found:** the file is renamed to `Artist - Title.ext` (name
   collisions get a `(1)`, `(2)`, … suffix). Playback is not interrupted —
   only the app's internal path tracking and the window title are updated.
6. **No match, silence, or any error:** nothing on disk is touched; the
   status bar reports what happened.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'numpy'` | `pip install numpy` |
| `Failed building wheel for shazamio-core` (Rust/`link.exe` errors) | Use the pinned `shazamio-core==1.2.0` + `--no-deps` install described above, instead of compiling from source |
| `ModuleNotFoundError: No module named 'audioop'` / `'pyaudioop'` | `pip install audioop-lts` (Python 3.13 only) |
| Icon build error: `FileNotFoundError: Icon input file ... not found` | Make sure `app.ico` exists in the folder you're running PyInstaller from, or drop `--icon=app.ico` from the command |
| `pyinstaller: error: the following arguments are required: scriptname` | You forgot to pass the script filename — run the command from the folder containing `video_studio_player.py` and include it at the end of the command |
| App builds but can't play video / export / recognize songs | Copy `ffmpeg.exe`, `ffprobe.exe`, and `libmpv-2.dll` into the `dist\VideoStudioPlayer\` folder next to the `.exe` |

## License

Add your license of choice here.

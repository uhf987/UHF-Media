@echo off
REM icon.ico, video_studio_player.py ve libmpv-2.dll AYNI klasorde olmali.
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name HuluMedia ^
  --icon=icon.ico ^
  --add-data "icon.ico;." ^
  --add-binary "libmpv-2.dll;." ^
  video_studio_player.py
pause

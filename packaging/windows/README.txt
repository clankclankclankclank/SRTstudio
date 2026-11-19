SRT Studio 0.1.0 | Windows x64
==============================
A local Whisper workbench for batch transcription. Your media stays on your PC.

FIRST SETUP (internet required)
1. Extract this ZIP to a normal, writable folder. Do not run the launchers from inside the ZIP.
2. Install 64-bit Python 3.11 or 3.12 and FFmpeg if you do not already have them.
   Python: https://www.python.org/downloads/windows/
   FFmpeg: https://ffmpeg.org/download.html (add the bin folder to PATH)
3. Run Install-SRT-Studio.cmd. Choose NVIDIA GPU or CPU. Current NVIDIA drivers
   are needed for the CUDA edition. The CPU option does not need a GPU.
4. Choose to prepare a model, or run Prepare-Offline.cmd separately.
   Turbo is recommended for NVIDIA. Small is less demanding on CPU.

EVERYDAY USE
Open Start-SRT-Studio.cmd. A workbench opens in your own browser at 127.0.0.1.
Choose files, run the batch, and export SRT, WebVTT, or TXT. The same model
works offline after preparation. Switching to an uncached model requires a
download. Keep the models folder intact. The app never uploads your media.

This is a source-based Windows package, not a standalone installer. Python,
FFmpeg, dependencies and model weights are not bundled. To move the prepared
app to a different PC, run setup there and copy the models folder afterward.

Site: https://clankclankclankclank.github.io/SRTstudio/
Source and releases: https://github.com/clankclankclankclank/SRTstudio/releases

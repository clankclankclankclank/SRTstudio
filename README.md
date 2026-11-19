# SRT Studio

**Your audio. In captions.** A local subtitle workbench for creators who process a folder of voiceovers at a time.

The Python app opens a browser interface on your machine. Whisper runs locally, on NVIDIA CUDA when available or on the CPU. The workbench does not upload your media to a remote service. Whisper may download the chosen model on first use.

## Quick start

You need Python 3.10 or later and FFmpeg on PATH. For GPU acceleration, use the [PyTorch installer](https://pytorch.org/get-started/locally/) to select a CUDA build compatible with your NVIDIA GPU and driver before installing the app requirements.

```powershell
git clone https://github.com/clankclankclankclank/SRTstudio.git
cd SRTstudio
python -m pip install -r requirements.txt
python srt_studio.py
```

Open the terminal URL if your browser does not launch. Use **Exit Studio** or press **Ctrl+C** in the terminal when you're done.

If you're migrating from the original GPU script, put `settings_whisper_gpu.json` beside `srt_studio.py` to carry over its presets. Keep this file out of Git.

## Workflow

1. Add files or a folder, then reorder the queue if needed.
2. Set the model, language, and output formats.
3. Start the batch and open **Exports** when it finishes.

**Browse files** or **Add folder** lets Studio preserve the source directory. Browser drag-and-drop does not provide the original path, so those files export to `Downloads/SRT Studio` by default, or into the custom output folder if selected. Subtitle formats are SRT, WebVTT, and plain text.

## Project layout

| Path | Purpose |
| --- | --- |
| `srt_studio.py` | Standalone local workbench and Whisper runner |
| `docs/` | GitHub Pages homepage, branding, and interactive sample captions |
| `tests/smoke.py` | Simulated-engine integration test for the local API and export workflow |

The homepage's sample is interactive, but does not transcribe uploaded files. To publish it, select **Settings → Pages → Deploy from a branch → main → /docs**.

## Development

```bash
python tests/smoke.py
python -m http.server 8000 --bind 127.0.0.1 --directory docs
```

The smoke test exercises queue ordering, cancellation, CUDA telemetry, settings persistence, SRT/VTT/TXT exports, and local server shutdown with a simulated engine. It does not measure real CUDA performance. The static homepage is then available locally at `http://127.0.0.1:8000`.

## Roadmap

- Split out the app UI and move the workbench to React without changing the batch workflow.
- Compare PyTorch and faster-whisper on the same short and long clips. Measure cold start, warm batch time, peak VRAM, and caption quality before changing defaults.
- Package the Windows edition and add an in-browser short-clip demo with a separate WebGPU engine.

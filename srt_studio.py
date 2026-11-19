#!/usr/bin/env python3
"""SRT Studio | offline-first transcription workbench.

Run: python srt_studio.py
Requires Python 3.10+, openai-whisper, PyTorch, and FFmpeg on PATH.
Install: python -m pip install -U openai-whisper
NVIDIA: install a CUDA-enabled PyTorch build that matches your GPU and driver.
Native file/folder pickers use Python's tkinter. Browser drop works without it.

No Node, web server setup, CDN, or external frontend assets. The UI runs
in your browser on loopback. Whisper may download weights on first use.
Close the background process with Exit Studio in the app or Ctrl+C.
"""

from __future__ import annotations

import argparse
import gc
import hmac
import importlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
import webbrowser

BASE = Path(__file__).resolve().parent
SETTINGS = BASE / "settings_whisper_gpu.json"
LOG_FILE = BASE / "logs" / "subtitle_generator.log"
NAME = "SRT Studio"
MEDIA = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".opus",
         ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
ESTIMATE = {"tiny": 1.0, "base": 1.2, "small": 2.5, "medium": 5.5,
            "turbo": 6.5, "large": 10.5}
FALLBACK_MODELS = ["tiny", "tiny.en", "base", "base.en", "small", "small.en",
                   "medium", "medium.en", "large", "large-v1", "large-v2", "large-v3", "turbo"]
LANGS = {
    "Auto-detect": None, "English": "en", "Chinese": "zh", "Japanese": "ja", "Korean": "ko",
    "Spanish": "es", "French": "fr", "German": "de", "Russian": "ru", "Portuguese": "pt",
    "Italian": "it", "Dutch": "nl", "Polish": "pl", "Turkish": "tr", "Arabic": "ar",
    "Hindi": "hi", "Ukrainian": "uk", "Vietnamese": "vi", "Thai": "th", "Indonesian": "id",
}
PRESETS = {"Fast": (1, 1, 0.0), "Balanced": (5, 5, 0.0),
           "Accurate": (5, 5, (0.0, 0.2, 0.4, 0.6, 0.8, 1.0))}
FORMATS = ("srt", "vtt", "txt")
DEFAULTS = dict(
    model="turbo", language="Auto-detect", task="transcribe", no_loop=True,
    fmt_srt=True, fmt_vtt=False, fmt_txt=False, out_mode="Next to source", out_dir="",
    overwrite=False, open_when_done=False, device="auto", precision="auto",
    preset="Balanced", cpu_threads=str(os.cpu_count() or 4), keep_model_loaded=True,
)
LOG = logging.getLogger("srt_studio")
NATIVE = r"""
import json, sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tk.TclError:
    pass
try:
    mode, initial = sys.argv[1:3]
    opts = {"parent": root}
    if initial:
        opts["initialdir"] = initial
    if mode == "files":
        patterns = tuple("*" + ext for ext in sys.argv[3:])
        result = list(filedialog.askopenfilenames(title="Add media to SRT Studio",
                      filetypes=[("Audio & video", patterns), ("All files", "*")], **opts))
    else:
        result = filedialog.askdirectory(title="Choose output folder" if mode == "output"
                                         else "Add a folder of media", **opts)
    print(json.dumps(result))
finally:
    root.destroy()
"""


class InputError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Cancelled(Exception):
    pass


def elapsed(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02}m {seconds:02}s" if hours else (
        f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s")


def stamp(t, sep):
    milli = max(0, round(t * 1000))
    h, milli = divmod(milli, 3_600_000)
    m, milli = divmod(milli, 60_000)
    s, milli = divmod(milli, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{milli:03}"


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False,
                                     dir=path.parent, prefix=".whisper-", suffix=".tmp") as f:
        temp = Path(f.name)
        try:
            f.write(data)
            f.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def write_outputs(segments, targets, overwrite):
    for fmt, path in targets.items():
        if not overwrite and path.exists():
            continue
        if fmt == "txt":
            content = "\n".join(seg["text"].strip() for seg in segments) + "\n"
        else:
            vtt = fmt == "vtt"
            sep = "." if vtt else ","
            cues = []
            for i, seg in enumerate(segments, 1):
                timing = f"{stamp(seg['start'], sep)} --> {stamp(seg['end'], sep)}"
                body = seg["text"].strip()
                cues.append(f"{timing}\n{body}\n\n" if vtt else f"{i}\n{timing}\n{body}\n\n")
            content = ("WEBVTT\n\n" if vtt else "") + "".join(cues)
        atomic_write(path, content)


def reveal(path):
    if not path.exists():
        raise InputError("That file or folder is no longer available.")
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def default_drop_folder():
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / "SRT Studio"


class Studio:
    def __init__(self):
        self.lock = threading.RLock()
        self.picker_lock = threading.Lock()
        self.cancel = threading.Event()
        self.stop = threading.Event()
        self.cache = tempfile.TemporaryDirectory(prefix="srt-studio-")
        self.rows = []
        self.events = deque(maxlen=175)
        self.event_seq = 0
        self.row_rev = 0
        self.worker = None
        self.batch = self.empty_batch()
        self.config = dict(DEFAULTS)
        self.models = list(FALLBACK_MODELS)
        self.devices = []
        self.engine = {"state": "checking", "torch": "", "cuda": "", "error": "", "ffmpeg": False,
                       "vram": None, "vram_device": None, "precise_progress": False}
        self.active_device = "cpu"
        self.output_dir = None
        self.whisper = self.torch = None
        self.progress_callback = None
        self.cached_key = self.cached_model = None
        try:
            raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
            self.config = self.validate(raw, strict=False)
        except (OSError, ValueError, InputError):
            pass
        self.activity("Studio launched.")
        threading.Thread(target=self.probe, name="whisper-engine", daemon=True).start()

    @staticmethod
    def empty_batch():
        return dict(phase="idle", message="Ready when you are", index=0, total=0,
                    completed=0, file_progress=None, eta=None, file="", done=0,
                    failed=0, skipped=0, cancelled=0)

    def activity(self, message, tone="neutral"):
        with self.lock:
            self.event_seq += 1
            self.events.append(dict(id=self.event_seq, time=time.strftime("%H:%M:%S"),
                                    message=str(message), tone=tone))
        LOG.info("%s", message)

    def probe(self):
        ffmpeg = bool(shutil.which("ffmpeg"))
        try:
            torch = importlib.import_module("torch")
            whisper = importlib.import_module("whisper")
            models = list(whisper.available_models())
            devices = []
            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    p = torch.cuda.get_device_properties(i)
                    devices.append(dict(index=i, name=p.name, total=round(p.total_memory / 1024**3, 2),
                                        capability=f"{p.major}.{p.minor}"))
                try:
                    torch.backends.cudnn.benchmark = True
                    if any(int(d["capability"].split(".")[0]) >= 8 for d in devices):
                        torch.backends.cuda.matmul.allow_tf32 = True
                        torch.backends.cudnn.allow_tf32 = True
                        torch.set_float32_matmul_precision("high")
                except Exception:
                    LOG.debug("Optional GPU tuning unavailable", exc_info=True)
            self.whisper, self.torch = whisper, torch
            hook = self.install_hook()
            with self.lock:
                self.models = models or list(FALLBACK_MODELS)
                self.devices = devices
                if self.config["model"] not in self.models:
                    self.config["model"] = next((m for m in ("turbo", "medium") if m in self.models),
                                                self.models[0])
                if self.config["device"] not in self.valid_devices():
                    self.config["device"] = "auto"
                self.engine.update(state="ready" if ffmpeg else "error", torch=str(torch.__version__),
                                   cuda=str(getattr(torch.version, "cuda", "") or ""), ffmpeg=ffmpeg,
                                   precise_progress=hook,
                                   error="" if ffmpeg else "FFmpeg is not on PATH. Install it and restart Studio.")
            if not ffmpeg:
                self.activity(self.engine["error"], "error")
            self.activity(f"PyTorch {torch.__version__} · " +
                          (f"{len(devices)} CUDA device(s) detected" if devices else "CPU mode"))
            if not hook:
                self.activity("Live percentages unavailable with this Whisper build.", "warn")
        except Exception as exc:
            with self.lock:
                self.engine.update(state="error", ffmpeg=ffmpeg,
                                   error=f"Whisper could not start: {exc}. Install openai-whisper and PyTorch, then restart.")
            self.activity(self.engine["error"], "error")
            return

        while not self.stop.is_set():
            with self.lock:
                pref = self.active_device if self.busy() else self.device(self.config["device"])
            idx = self.device_index(pref)
            used = None
            if idx is not None:
                try:
                    free, total = torch.cuda.mem_get_info(idx)
                    used = round((total - free) / 1024**3, 2)
                except Exception:
                    pass
            with self.lock:
                self.engine.update(vram=used, vram_device=idx if used is not None else None)
            self.stop.wait(2.2)

    def install_hook(self):
        try:
            module = importlib.import_module("whisper.transcribe")
            from tqdm import tqdm
            studio = self

            class QuietProgress(tqdm):
                def __init__(self, *args, **kwargs):
                    self.sink = open(os.devnull, "w")
                    kwargs.update(file=self.sink)
                    super().__init__(*args, **kwargs)

                def update(self, n=1):
                    super().update(n)
                    if studio.progress_callback and self.total:
                        studio.progress_callback(min(self.n / self.total, 1.0))

                def close(self):
                    try:
                        super().close()
                    finally:
                        self.sink.close()

            module.tqdm = types.SimpleNamespace(tqdm=QuietProgress)
            return True
        except Exception:
            LOG.debug("Progress hook unavailable", exc_info=True)
            return False

    def valid_devices(self):
        return ["auto", "cpu"] + (["cuda"] + [f"cuda:{d['index']}" for d in self.devices]
                                 if self.devices else [])

    def device(self, pref):
        return ("cuda:0" if pref in ("auto", "cuda") else pref) if self.devices and pref != "cpu" else "cpu"

    @staticmethod
    def device_index(device):
        return int(device.split(":")[-1]) if device.startswith("cuda:") else None

    def busy(self):
        return self.batch["phase"] in {"loading", "running", "cancelling", "preparing"}

    def validate(self, cfg, strict=True):
        if not isinstance(cfg, dict):
            raise InputError("Invalid configuration.")
        result = dict(DEFAULTS)
        options = dict(model=self.models, language=LANGS, task=("transcribe", "translate"),
                       precision=("auto", "fp16", "fp32"), preset=PRESETS,
                       out_mode=("Next to source", "Custom folder"))
        if strict:
            options["device"] = self.valid_devices()
        for key, default in DEFAULTS.items():
            value = cfg.get(key, default)
            valid = (value in options[key] if key in options and isinstance(value, str) else
                     isinstance(value, bool) if isinstance(default, bool) else
                     isinstance(value, str) and len(value) < 4096 and key not in options)
            if key == "cpu_threads" and isinstance(value, (str, int)):
                valid = str(value).isdigit() and 1 <= int(value) <= 4096
                value = str(value)
            if valid:
                result[key] = value
            elif strict:
                raise InputError(f"Invalid setting: {key.replace('_', ' ')}.")
        return result

    def save(self, cfg):
        self.config = cfg
        try:
            atomic_write(SETTINGS, json.dumps(cfg, indent=2) + "\n")
        except OSError as exc:
            self.activity(f"Settings could not be saved: {exc}", "warn")

    def snapshot(self, since):
        with self.lock:
            return dict(config=self.config.copy(), models=list(self.models), languages=list(LANGS),
                        devices=list(self.devices), engine=self.engine.copy(), busy=self.busy(),
                        batch=self.batch.copy(), rev=self.row_rev, drop_folder=str(default_drop_folder()),
                        rows=[{k: v for k, v in row.items() if k not in ("path", "staging")} for row in self.rows],
                        events=[e for e in self.events if e["id"] > since],
                        logging=LOG_FILE.is_file(), output_dir=str(self.output_dir or ""))

    def require_idle(self):
        if self.busy():
            raise InputError("Stop the current batch before changing its queue.", 409)

    def add_paths(self, paths):
        with self.lock:
            self.require_idle()
            existing = {os.path.normcase(str(row["path"])) for row in self.rows if not row["staging"]}
            count = 0
            for path in paths:
                if path.suffix.lower() not in MEDIA or not path.is_file():
                    continue
                path = path.resolve()
                key = os.path.normcase(str(path))
                if key in existing:
                    continue
                existing.add(key)
                self.add_row(path, path.name, str(path.parent), False)
                count += 1
            self.activity(f"Added {count} media file{'s' if count != 1 else ''}." if count else
                          "No new supported media files found.")
            return count

    def add_row(self, path, name, source, staging):
        self.rows.append(dict(id=secrets.token_hex(8), name=name, source=source,
                              size=path.stat().st_size, ext=path.suffix[1:].upper(),
                              path=path, staging=staging, status="Queued", detail="", outputs=[]))
        self.row_rev += 1

    def picker(self, mode):
        if mode not in {"files", "folder", "output"}:
            raise InputError("Unknown dialog.")
        self.require_idle()
        if not self.picker_lock.acquire(blocking=False):
            raise InputError("A file picker is already open.", 409)
        try:
            initial = str(Path.home() / "Downloads")
            if not Path(initial).is_dir():
                initial = str(Path.home())
            if mode == "output":
                initial = self.config.get("out_dir") or initial
            kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            p = subprocess.run([sys.executable, "-c", NATIVE, mode, initial, *MEDIA],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
            if p.returncode:
                raise InputError("Native file picker unavailable. Drag media into Studio or use Browser picker.", 503)
            value = json.loads(p.stdout)
            if not value:
                return {}
            if mode == "output":
                with self.lock:
                    self.require_idle()
                    self.save({**self.config, "out_dir": value, "out_mode": "Custom folder"})
                return {"dir": value}
            paths = sorted(Path(value).rglob("*")) if mode == "folder" else map(Path, value)
            return {"added": self.add_paths(paths)}
        finally:
            self.picker_lock.release()

    def upload(self, name, size, stream):
        with self.lock:
            self.require_idle()
        if len(name) > 255 or not name or Path(name).suffix.lower() not in MEDIA:
            raise InputError("Unsupported media file.")
        if size <= 0:
            raise InputError("Empty media file.")
        base = Path(self.cache.name) / secrets.token_hex(10)
        base.mkdir()
        # A controlled temporary filename avoids platform-specific reserved names.
        path = base / ("media" + Path(name).suffix.lower())
        try:
            with path.open("wb") as f:
                remaining = size
                while remaining:
                    data = stream.read(min(1024 * 1024, remaining))
                    if not data:
                        raise InputError("The upload was interrupted.")
                    f.write(data)
                    remaining -= len(data)
            with self.lock:
                self.require_idle()
                self.add_row(path, name, "Browser import", True)
        except BaseException:
            shutil.rmtree(base, ignore_errors=True)
            raise
        return {"added": 1}

    def edit_queue(self, action, ids):
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise InputError("Invalid selection.")
        with self.lock:
            self.require_idle()
            selection = set(ids)
            if action in ("clear", "remove"):
                gone = [x for x in self.rows if action == "clear" or x["id"] in selection]
                self.rows = [r for r in self.rows if r not in gone]
                for row in gone:
                    if row["staging"]:
                        shutil.rmtree(row["path"].parent, ignore_errors=True)
                if gone:
                    self.activity(f"Removed {len(gone)} from the queue.")
                if not self.rows:
                    self.batch = self.empty_batch()
            elif action in ("up", "down"):
                up = action == "up"
                indexes = range(1, len(self.rows)) if up else range(len(self.rows)-2, -1, -1)
                for i in indexes:
                    j = i-1 if up else i+1
                    if self.rows[i]["id"] in selection and self.rows[j]["id"] not in selection:
                        self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            else:
                raise InputError("Unknown queue action.")
            self.row_rev += 1

    def open_target(self, body):
        kind = body.get("kind")
        with self.lock:
            if kind == "log":
                path = LOG_FILE
            elif kind == "destination":
                path = self.output_dir
            else:
                row = next((r for r in self.rows if r["id"] == body.get("id")), None)
                if row is None:
                    raise InputError("File is no longer in the queue.")
                if kind == "folder":
                    if not row["outputs"]:
                        raise InputError("This item has no exported output.")
                    path = Path(row["outputs"][0]["path"]).parent
                elif kind == "file":
                    if row["staging"] and not row["outputs"]:
                        raise InputError("This file was imported through the browser. Open the original from your file manager.")
                    path = Path(row["outputs"][0]["path"]) if row["outputs"] else row["path"]
                else:
                    raise InputError("Unknown item.")
        if path is None:
            raise InputError("No output folder yet.")
        try:
            reveal(path)
        except OSError as exc:
            raise InputError(f"Could not open this path: {exc}") from exc

    def start(self, data):
        with self.lock:
            self.require_idle()
            if self.engine["state"] != "ready":
                raise InputError(self.engine["error"] or "The engine is still starting.", 409)
            if not self.rows:
                raise InputError("Add at least one media file.")
            cfg = self.validate(data.get("config"))
            if not any(cfg[f"fmt_{fmt}"] for fmt in FORMATS):
                raise InputError("Select at least one export format.")
            if cfg["out_mode"] == "Custom folder" and not cfg["out_dir"].strip():
                raise InputError("Enter or browse for a custom output folder.")
            if cfg["task"] == "translate" and (cfg["model"] == "turbo" or cfg["model"].endswith(".en")):
                raise InputError("Choose a multilingual model, such as medium, to translate into English.")
            device = self.device(cfg["device"])
            idx = self.device_index(device)
            if idx is not None:
                model = cfg["model"].split(".")[0].split("-v")[0]
                need = ESTIMATE.get(model, 6.5)
                available = self.devices[idx]["total"]
                if need > available - 1 and data.get("allow_low_vram") is not True:
                    return {"confirm": f"{cfg['model']} needs roughly {need:g} GB plus headroom. "
                                       f"The selected GPU has {available:g} GB. Continue anyway?"}
            self.save(cfg)
            self.active_device = device
            self.cancel.clear()
            self.batch = self.empty_batch()
            self.batch.update(phase="preparing", message="Preparing batch", total=len(self.rows))
            self.output_dir = None
            targets = list(self.rows)
            for row in targets:
                row.update(status="Queued", detail="", outputs=[])
            self.row_rev += 1
            self.worker = threading.Thread(target=self.run, args=(targets, cfg, device),
                                           name="whisper-batch", daemon=True)
            self.worker.start()
            return {}

    def cancel_run(self):
        with self.lock:
            if self.busy():
                self.cancel.set()
                self.batch.update(phase="cancelling", message="Stopping after the current operation")
                self.activity("Stop requested.", "warn")

    def mark(self, row, status, detail="", outputs=None):
        with self.lock:
            row.update(status=status, detail=detail, outputs=outputs or [])
            self.row_rev += 1

    def set_batch(self, **data):
        with self.lock:
            if self.cancel.is_set() and data.get("phase") in {"loading", "running"}:
                data["phase"] = "cancelling"
                data["message"] = "Stopping after the current operation"
            self.batch.update(data)

    def free_gpu(self, device):
        if self.device_index(device) is not None:
            try:
                with self.torch.cuda.device(device):
                    self.torch.cuda.empty_cache()
            except Exception:
                LOG.debug("Could not release CUDA allocator cache", exc_info=True)

    def get_model(self, cfg, device):
        name = cfg["model"]
        if device == "cpu":
            self.torch.set_num_threads(int(cfg["cpu_threads"]))
        key = (name, device)
        if key != self.cached_key:
            old = self.cached_key
            self.cached_key = self.cached_model = None
            if old:
                gc.collect()
                self.free_gpu(old[1])
            self.set_batch(phase="loading", message=f"Loading {name} on {device}", file_progress=None)
            self.activity(f"Loading {name} on {device}. First use may download weights.")
            self.cached_model = self.whisper.load_model(name, device=device)
            self.cached_key = key
        return self.cached_model

    def run(self, rows, cfg, device):
        started = time.monotonic()
        counts = dict(done=0, skipped=0, failed=0, cancelled=0)
        last_dir, model, fatal = None, None, None
        fp16 = device.startswith("cuda:") and cfg["precision"] != "fp32"
        if not fp16 and cfg["precision"] == "fp16":
            self.activity("FP16 is unavailable on CPU. Using FP32.", "warn")
        beam, best, temp = PRESETS[cfg["preset"]]
        self.activity(f"Batch started · {len(rows)} files · {device} · {'FP16' if fp16 else 'FP32'}")
        formats = [fmt for fmt in FORMATS if cfg[f"fmt_{fmt}"]]
        claimed = set()
        try:
            for index, row in enumerate(rows, 1):
                current = time.monotonic()
                self.set_batch(index=index, file=row["name"], file_progress=None, eta=None)
                try:
                    if self.cancel.is_set():
                        raise Cancelled
                    if fatal:
                        raise InputError(fatal)
                    out = (Path(cfg["out_dir"]).expanduser() if cfg["out_mode"] == "Custom folder"
                           else default_drop_folder() if row["staging"] else row["path"].parent)
                    out = out.resolve()
                    out.mkdir(parents=True, exist_ok=True)
                    stem = Path(row["name"]).stem
                    candidate, counter = stem, 2
                    while (os.path.normcase(str(out / candidate))) in claimed:
                        candidate = f"{stem} ({counter})"
                        counter += 1
                    claimed.add(os.path.normcase(str(out / candidate)))
                    targets = {fmt: out / f"{candidate}.{fmt}" for fmt in formats}
                    files = [{"format": fmt, "path": str(path)} for fmt, path in targets.items()]
                    if not cfg["overwrite"] and all(path.is_file() for path in targets.values()):
                        self.mark(row, "Skipped", "Exports already exist", files)
                        last_dir = out
                        counts["skipped"] += 1
                        continue

                    if model is None:
                        try:
                            if self.cancel.is_set():
                                raise Cancelled
                            model = self.get_model(cfg, device)
                        except Cancelled:
                            raise
                        except Exception as exc:
                            fatal = f"Model load failed: {exc}"
                            raise
                    if self.cancel.is_set():
                        raise Cancelled
                    current = time.monotonic()
                    last_update = 0.0
                    self.mark(row, "Running", "Transcribing")
                    self.set_batch(phase="running", message="Transcribing",
                                   file_progress=0.0 if self.engine["precise_progress"] else None)

                    def progress(fraction):
                        nonlocal last_update
                        if self.cancel.is_set():
                            raise Cancelled
                        now = time.monotonic()
                        if now - last_update < 0.16 and fraction < 1:
                            return
                        last_update = now
                        eta = (now - current) * (1 - fraction) / fraction if 0.03 < fraction < 1 else None
                        self.set_batch(file_progress=fraction, eta=elapsed(eta) if eta is not None else None)

                    self.progress_callback = progress
                    result = model.transcribe(
                        audio=str(row["path"]), language=LANGS[cfg["language"]], task=cfg["task"],
                        verbose=False, fp16=fp16, beam_size=beam, best_of=best, temperature=temp,
                        no_speech_threshold=0.6, condition_on_previous_text=not cfg["no_loop"],
                    )
                    if self.cancel.is_set():
                        raise Cancelled
                    segments = result["segments"]
                    write_outputs(segments, targets, cfg["overwrite"])
                    duration = segments[-1]["end"] if segments else 0
                    took = time.monotonic() - current
                    info = (f"{len(segments)} cues · {elapsed(duration)} media · "
                            f"{duration/max(0.001, took):.1f}× · {result.get('language', '?').upper()}")
                    self.mark(row, "Done", info, files)
                    self.set_batch(file_progress=1)
                    last_dir = out
                    counts["done"] += 1
                    self.activity(f"{row['name']} · {info}", "good")
                except Cancelled:
                    self.mark(row, "Cancelled")
                    counts["cancelled"] += 1
                except Exception as exc:
                    detail = fatal or str(exc)
                    LOG.debug("Batch item failed", exc_info=True)
                    self.mark(row, "Failed", detail)
                    counts["failed"] += 1
                    self.activity(f"{row['name']} · {detail}", "error")
                finally:
                    self.progress_callback = None
                    self.free_gpu(device)
                    self.set_batch(completed=index, **counts)
        finally:
            model = None
            if not cfg["keep_model_loaded"]:
                self.cached_key = self.cached_model = None
                gc.collect()
                self.free_gpu(device)
            duration = elapsed(time.monotonic() - started)
            phase = "cancelled" if counts["cancelled"] else "finished"
            headline = "Batch stopped" if phase == "cancelled" else "Batch complete"
            if counts["failed"]:
                headline = "Batch finished with errors"
            info = " · ".join(f"{count} {key}" for key, count in counts.items() if count)
            self.activity(f"{headline} in {duration} · {info}", "warn" if counts["failed"] else "good")
            if cfg["open_when_done"] and last_dir:
                try:
                    reveal(last_dir)
                except (OSError, InputError) as exc:
                    self.activity(f"Could not open folder: {exc}", "warn")
            with self.lock:
                self.output_dir = last_dir
                self.batch.update(phase=phase, message=headline, file_progress=None, eta=None, file="")


PAGE = r'''
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SRT Studio — Transcription Workbench</title>
<style>:root{color-scheme:dark;--bg:#101213;--rail:#111314;--panel:#17191a;--raised:#1c1f20;--inset:#121415;--line:#2a2e2d;--line2:#393e3a;--text:#f1f3ee;--muted:#a2aaa4;--sub:#818b83;--lime:#dbfa84;--mint:#a5e5a2;--amber:#f0c483;--red:#ffa5a7;--mono:"Cascadia Code","SFMono-Regular",Consolas,"Liberation Mono",monospace;--sans:"Segoe UI Variable","Inter","Segoe UI",system-ui,sans-serif;--rail-width:217px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans);min-width:320px}
button,input,select{font:inherit} button{color:inherit;cursor:pointer}
button:disabled{cursor:not-allowed} button[hidden], [hidden]{display:none!important}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--lime);outline-offset:3px}
a{color:var(--lime)} svg{display:block;width:18px;height:18px;flex:none}
i[data-icon]{display:inline-flex;align-items:center;justify-content:center;font-style:normal}
h1,h2,p{margin:0} .mono{font-family:var(--mono);letter-spacing:.035em}
.led{display:inline-block;width:7px;height:7px;color:var(--lime);background:currentColor;border-radius:50%;box-shadow:0 0 10px #c4f48475;flex:none}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.app{min-height:100vh;display:grid;grid-template-columns:var(--rail-width) minmax(0,1fr)}
.rail{position:sticky;top:0;height:100vh;background:var(--rail);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:24px 14px 16px;z-index:3}
.identity{display:flex;gap:11px;align-items:center;padding:0 6px}
.brand-icon{width:40px;height:40px;background:var(--lime);color:#18200d;border-radius:11px;display:grid;place-items:center;box-shadow:0 2px 18px #c4f48414}
.brand-icon svg{width:23px;height:23px;stroke-width:2}
.identity strong{display:block;font-size:15px;line-height:1.1;font-weight:750;letter-spacing:-.035em;white-space:nowrap}
.identity span{display:block;margin-top:5px;color:var(--sub);font:9px/1 var(--mono);letter-spacing:.1em;white-space:nowrap}
.rail-group{margin-top:43px}
.rail-label{margin:0 8px 12px;color:var(--sub);font:10px var(--mono);letter-spacing:.13em}
nav{display:grid;gap:5px}
nav button,.rail-exit{width:100%;position:relative;display:flex;gap:11px;align-items:center;min-height:41px;padding:0 10px;border:1px solid transparent;border-radius:8px;background:transparent;text-align:left;font-weight:600;font-size:13px;color:var(--muted);transition:background .17s,color .17s}
nav button svg,.rail-exit svg{width:16px;height:16px}
nav button:hover,.rail-exit:hover{background:#1b1f1b;color:var(--text)}
nav button[aria-current=page]{color:var(--lime);background:#242a1d;border-color:#3a4530}
nav button[aria-current=page]::before{content:"";position:absolute;width:2px;height:16px;left:-1px;border-radius:2px;background:var(--lime)}
nav button b{margin-left:auto;border-radius:5px;background:#30362d;padding:2px 5px;font:10px var(--mono);color:#ced9bc}
.rail-bottom{margin-top:auto}
.hardware{background:#171a18;border:1px solid #2c3428;border-radius:10px;padding:12px 11px 10px;margin-bottom:9px;overflow:hidden}
.hardware-top{display:flex;align-items:center;gap:6px;color:#c0ccad;font:8.5px var(--mono);letter-spacing:.045em;white-space:nowrap}
.hardware-top svg{width:13px;height:13px;color:var(--lime)}
.hardware-top .led{width:5px;height:5px;margin-left:auto;box-shadow:none}
.led.wait{color:var(--amber)}.led.error{color:var(--red)}
.hardware-name{margin-top:8px;font-size:12px;font-weight:700;overflow-wrap:anywhere}
.hardware-meta{margin-top:3px;color:var(--sub);font-size:10px;overflow-wrap:anywhere}
.hardware .meter{margin:8px 0 5px}
.rail-exit{padding-left:8px;min-height:34px;font-size:11px}
.rail-exit svg{width:14px;height:14px}
.shell{min-width:0;display:flex;flex-direction:column}
.top{display:flex;min-height:62px;align-items:center;justify-content:space-between;padding:0 clamp(20px,3.05vw,48px);border-bottom:1px solid #252827}
.breadcrumb{display:flex;align-items:center;gap:11px;color:var(--sub);font-size:13px}
.breadcrumb strong{color:var(--text);font-weight:650}
.breadcrumb svg{width:12px;height:12px;color:#788379}
.local-pill{display:flex;align-items:center;gap:8px;padding:7px 10px;border:1px solid #30382b;background:#1b2018;border-radius:6px;color:#d1e0b9;font:10px var(--mono);letter-spacing:.07em}
.local-pill .led{width:5px;height:5px}
.main{width:100%;max-width:1640px;margin:0 auto;padding:25px clamp(20px,3.05vw,48px) 25px;flex:1;min-width:0}
.hero{height:221px;position:relative;isolation:isolate;display:flex;align-items:center;overflow:hidden;border:1px solid #373f30;border-radius:15px;background:radial-gradient(ellipse at 78% 135%,#344125 2%,#20271c 42%,#181d17 72%);margin-bottom:21px}
.hero::before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,#181d17 0%,#181d17f0 33%,#181d1710 67%);z-index:-1;pointer-events:none}
.hero-copy{padding:0 27px 0 30px;position:relative;z-index:1}
.overline{display:flex;align-items:center;gap:7px;color:var(--lime);font:10px var(--mono);letter-spacing:.145em;white-space:nowrap}
.overline svg{width:15px;height:15px}
.mini-arrow{color:#9aaa75}
.hero h1{font-size:clamp(36px,4.45vw,67px);line-height:1.04;letter-spacing:-.075em;font-weight:760;margin:9px 0 7px}
.hero h1 span{color:var(--lime)}
.hero p{color:#b1b9ab;font-size:13px;}
.hero-meta{display:flex;align-items:center;gap:11px;margin-top:20px;color:#c1d0ae;font:9.5px var(--mono);letter-spacing:.065em;white-space:nowrap}
.hero-meta span:not(.meta-divider){display:inline-flex;gap:6px;align-items:center}
.hero-meta svg{height:13px;width:13px;color:#a5be74}
.hero-meta b{font-weight:500} .meta-divider{width:1px;height:12px;background:#454d3d}
.hero-visual{position:absolute;inset:14px 16px 14px auto;width:51%;background:repeating-linear-gradient(90deg,#b8db8310 0px,#b8db8310 1px,transparent 1px,transparent 27px),repeating-linear-gradient(0deg,#b8db8310 0px,#b8db8310 1px,transparent 1px,transparent 25px);mask-image:linear-gradient(90deg,transparent,#000 13%,#000);opacity:.93;overflow:hidden}
.hero-visual::before{content:"";position:absolute;top:50%;width:100%;height:1px;background:#b9df8750}
.hero-visual::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,#d0fc861d,transparent);width:64px;transform:translateX(-65px);pointer-events:none;opacity:0}
.hero.running .hero-visual::after{animation:scan 2.7s linear infinite;opacity:1}
@keyframes scan{from{left:0}to{left:calc(100% + 65px)}}
.hero-visual svg{position:absolute;width:100%;height:100%;top:0;left:0}
.scope-label,.scope-scale{position:absolute;z-index:1;color:#b4c298;font:8px var(--mono);letter-spacing:.07em}
.scope-label{top:8px;right:9px}.scope-label span{color:#6f8257}
.scope-scale{inset:auto 9px 8px 16%;display:flex;justify-content:space-between;color:#9dab85}
.reticle{position:absolute;width:8px;height:8px;border-style:solid;border-color:#81946063}
.reticle.tl{top:0;left:0;border-width:1px 0 0 1px}.reticle.tr{top:0;right:0;border-width:1px 1px 0 0}.reticle.bl{bottom:0;left:0;border-width:0 0 1px 1px}.reticle.br{bottom:0;right:0;border-width:0 1px 1px 0}
.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(297px,330px);gap:17px;align-items:start}
.workcol{min-width:0}
.card{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:13px;overflow:hidden}
.card-head{min-height:77px;padding:14px 16px;display:flex;gap:12px;align-items:center;justify-content:space-between}
.card-id{display:flex;gap:10px;align-items:center;min-width:0}
.section-icon{display:grid;place-items:center;width:36px;height:36px;border-radius:9px;border:1px solid #373e32;background:#20251e;color:#d0e0b4;flex:none}
.section-icon svg{width:17px;height:17px}
.card h2{font-size:16px;line-height:1.15;font-weight:710;letter-spacing:-.025em;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.card-head p{color:var(--sub);font-size:11px;margin-top:4px}
.count{font:10px var(--mono);background:#212622;color:#bcc9af;border:1px solid #363e34;border-radius:5px;padding:2px 5px}
.button{border:1px solid transparent;display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:35px;padding:0 11px;white-space:nowrap;border-radius:7px;font-size:12px;font-weight:675;transition:filter .17s,transform .17s,background .17s,border-color .17s}
.button svg{width:15px;height:15px}
.button:not(:disabled):hover{filter:brightness(1.14)}
.button:not(:disabled):active{transform:translateY(1px)}
.button.soft{background:#202322;border-color:#333934;color:#d9ded7}
.button.soft:disabled{opacity:.42}
.button.browse{background:#2d3823;border-color:#4a5b37;color:#e1f6be}
.button.browse:disabled{opacity:.5}
.dropzone{margin:0 16px;min-height:115px;display:flex;align-items:center;gap:12px;padding:16px;border:1px dashed #4c5a3d;border-radius:10px;background:linear-gradient(120deg,#20271a,#191e17 82%);transition:background .18s,border-color .18s,box-shadow .18s;flex-wrap:wrap}
.dropzone:hover,.dropzone.dragging{border-color:#9dac77;background:#283321}
.dropzone.dragging{box-shadow:inset 0 0 0 1px var(--lime),0 0 24px #adff691c}
.drop-icon{display:grid;place-items:center;width:40px;height:40px;flex:none;border:1px solid #495940;border-radius:9px;background:#283024;color:var(--lime)}
.drop-icon svg{width:20px;height:20px}
.drop-title{font-weight:700;font-size:14px;line-height:1.2}
.drop-formats{font-size:9px;color:#a1ae91;margin-top:6px}
.drop-formats b{font-weight:400;color:#76846a}
.dropzone .browse{margin-left:auto}
.dropzone .browser-picker{width:100%;text-align:right}
.queue-toolbar{min-height:45px;display:flex;justify-content:space-between;align-items:center;padding:0 16px;gap:7px}
.queue-caption{font-size:9px;color:#a0a99e;letter-spacing:.10em;white-space:nowrap}
.row-actions{display:flex;gap:3px;align-items:center}
.row-actions>span{height:15px;width:1px;background:#343932;margin:0 3px}
.icon-button,.quiet{display:inline-flex;align-items:center;justify-content:center;border:1px solid transparent;background:none;color:#b5bfb3;border-radius:6px;min-height:28px}
.icon-button{width:28px;padding:0;flex:none}
.icon-button svg{width:15px;height:15px}
.quiet{font-size:11px;padding:0 5px;white-space:nowrap}
.icon-button:disabled,.quiet:disabled{opacity:.40}
.icon-button:hover:not(:disabled),.quiet:hover:not(:disabled){color:var(--lime);background:#242b22;border-color:#384238}
.table-shell{border-top:1px solid var(--line);min-width:0}
.queue-table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}
.queue-table th{text-align:left;background:#1c201d;color:#aeb7aa;font:9px var(--mono);text-transform:uppercase;letter-spacing:.085em;height:32px}
.queue-table th:nth-child(3){width:107px}
.select-col{width:43px;text-align:center!important}
.row-open-head{width:38px}
.queue-table td{border-top:1px solid #262a27;padding:9px 0}
.queue-table tr[data-id]{transition:background .16s}
.queue-table tr[data-id]:hover,.queue-table tr.selected{background:#20261d}
input[type=checkbox],input[type=radio]{accent-color:var(--lime)}
.queue-table input[type=checkbox]{width:13px;height:13px;margin:0;vertical-align:middle;cursor:pointer}
.file-cell{display:flex;gap:8px;align-items:center;min-width:0;padding-right:7px}
.file-cell .file-icon{display:grid;place-items:center;background:#252c23;color:#c1d3aa;border:1px solid #384231;border-radius:6px;width:29px;height:29px;flex:none}
.file-cell .file-icon svg{width:14px;height:14px}
.file-name{font-size:12px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-info{color:var(--sub);font-size:9.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-text{min-width:0}
.status{display:inline-flex;align-items:center;gap:5px;border:1px solid #384138;background:#232924;color:#bcc7bb;border-radius:5px;padding:3px 6px;font:9px var(--mono);white-space:nowrap}
.status::before{content:"";width:5px;height:5px;background:currentColor;border-radius:50%}
.status.d{color:#b5e6ab;background:#233322;border-color:#39583a}.status.r{color:var(--lime);background:#2d3921;border-color:#465932}.status.f{color:var(--red);background:#342426;border-color:#593b3d}.status.c,.status.s{color:#edc78d;background:#302a20;border-color:#514332}
.empty{display:flex;justify-content:center;align-items:center;gap:12px;min-height:103px;padding:14px;color:var(--sub)}
.empty>i{width:33px;height:33px;background:#202621;border:1px solid #323a32;border-radius:8px;color:#98ab91}
.empty strong,.empty span{display:block}.empty strong{font-size:12px;color:#cbd3c9}.empty span{font-size:11px;margin-top:2px}
.tabs{margin:0 13px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:3px;padding:4px;background:#121514;border:1px solid #2a302b;border-radius:8px}
.tabs button{background:none;color:#a2aaa1;border:1px solid transparent;font-size:11px;font-weight:650;border-radius:5px;min-height:29px}
.tabs button:hover{color:var(--text)}
.tabs [aria-selected=true]{background:#262e23;border-color:#394436;color:#d9e8d0}
fieldset{padding:0;margin:0;border:0;min-width:0}
.settings-panel{padding:13px 15px 12px;display:grid;gap:11px}
.control{display:grid;gap:5px;font-size:11px;color:#d6ddd3;font-weight:620;min-width:0}
.control.two{grid-template-columns:1fr 1fr;gap:8px}
.control.two label{display:grid;gap:5px;min-width:0}
select,.settings-panel input:not([type=radio]):not([type=checkbox]){background-color:#1c211d;border:1px solid #363e35;border-radius:7px;color:#e7ede4;min-height:35px;min-width:0;width:100%;padding:0 9px;font-size:12px}
select{appearance:none;padding-right:28px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath d='m1 1 5 5 5-5' fill='none' stroke='%23a5b19e' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-size:10px 7px;background-position:right 10px center}
select:disabled,.settings-panel input:disabled{opacity:.64} option{background:#1c211d}
.segmented{display:flex;gap:5px}
.segmented label{position:relative;flex:1;min-width:0}
.segmented input,.switch input{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;cursor:pointer}
.segmented label span{display:flex;align-items:center;justify-content:center;gap:5px;min-height:33px;border:1px solid #363e35;background:#1a1e1b;border-radius:6px;color:#aeb9ac;font-size:10.5px;white-space:nowrap}
.segmented svg{width:13px;height:13px}
.segmented input:focus-visible+span,.switch:has(input:focus-visible){outline:2px solid var(--lime);outline-offset:2px}
.segmented input:checked+span{border-color:#4a5c3c;background:#293523;color:#e0f3cc}
.formats span{font-family:var(--mono)}
.switch{position:relative;display:flex;align-items:center;justify-content:space-between;gap:7px;padding:7px 9px;border:1px solid #2d352e;border-radius:7px;background:#1a1e1b;min-height:44px}
.switch strong,.switch small{display:block}.switch strong{font-size:11px;font-weight:650}.switch small{font-size:9.5px;color:#9ca699}
.switch>i{width:29px;height:16px;border:1px solid #465047;border-radius:40px;background:#2b322b;flex:none;transition:background .15s}
.switch>i::before{content:"";display:block;width:10px;height:10px;margin:2px;border-radius:50%;background:#adb7a9;transition:transform .17s}
.switch input:checked+i{background:#5b6f43;border-color:#7e9660}
.switch input:checked+i::before{transform:translateX(13px);background:var(--lime)}
.switch:has(input:hover){border-color:#485747}
.path-row{display:flex;gap:5px;align-items:center}
.drop-hint{color:var(--sub);font-size:10px;overflow-wrap:anywhere}
.config-foot{display:flex;align-items:center;gap:7px;min-height:36px;border-top:1px solid var(--line);padding:7px 15px;background:#181c18;color:#acb9a2;font:9.5px var(--mono)}
.config-foot svg{height:12px;width:12px;color:#a8bda0}
.activity-list,.export-list{border-top:1px solid var(--line);padding:4px 15px 10px;max-height:480px;overflow:auto;scrollbar-color:#414b40 transparent}
.log-row,.export-row{display:flex;align-items:center;gap:10px;min-height:42px;padding:8px 0;border-bottom:1px solid #292f29;overflow-wrap:anywhere}
.log-row:last-child,.export-row:last-child{border-bottom:0}
.log-row time{font:10px var(--mono);color:var(--sub);white-space:nowrap}
.log-row span{font-size:12px;min-width:0}
.log-row i{height:6px;width:6px;border-radius:50%;background:#83a17d;flex:none}.log-row i.warn{background:var(--amber)}.log-row i.error{background:var(--red)}.log-row i.good{background:var(--lime)}
.export-row .file-text{flex:1}
.export-tags{display:flex;gap:3px;flex-wrap:wrap}
.export-tags b{font:9px var(--mono);color:var(--lime);border:1px solid #3e4b37;background:#252e20;border-radius:4px;padding:3px 4px}
.inline-alert{background:#282021;border:1px solid #513a3d;border-radius:8px;margin-top:15px;padding:9px 12px;color:#ffc4c4;font-size:12px}
.transport{position:sticky;bottom:0;z-index:2;border-top:1px solid #313833;background:#141816f2;backdrop-filter:blur(14px)}
.transport-inner{max-width:1640px;margin:0 auto;min-height:76px;padding:10px clamp(20px,3.05vw,48px);display:grid;grid-template-columns:minmax(128px,1.35fr) minmax(105px,1fr) auto;gap:clamp(10px,2vw,26px);align-items:center}
.transport-status{display:flex;gap:9px;align-items:center;min-width:0}
.transport-icon{display:grid;place-items:center;width:36px;height:36px;background:#222b20;border:1px solid #3b4835;color:var(--lime);border-radius:8px;flex:none}
.transport-copy{min-width:0}
#batch-title{font-size:12px;font-weight:750;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
#batch-sub{font-size:10px;color:var(--sub);margin-top:2px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.progress-label{display:flex;justify-content:space-between;color:#b0bcae;font-size:9px;margin-bottom:6px;gap:4px}
.meter{height:5px;border-radius:8px;overflow:hidden;background:#2c342c}
.meter>div{width:0;height:100%;background:linear-gradient(90deg,#8fac55,var(--lime));border-radius:inherit;transition:width .23s}
.meter>div.indeterminate{width:30%!important;animation:loading 1.65s ease-in-out infinite}
@keyframes loading{0%{transform:translateX(-105%)}100%{transform:translateX(445%)}}
.transport-actions{display:flex;gap:6px}
.primary{background:var(--lime);border-color:#e4ff9e;color:#18200e;min-height:42px;padding:0 11px;box-shadow:0 3px 15px #bce87a18;font-weight:760}
.primary:disabled{background:#262f22;border-color:#394433;color:#a4b49a;box-shadow:none}
.primary kbd{font:14px var(--mono);padding:1px 5px;border:1px solid #a3bb72;border-radius:4px;margin-left:4px}
.primary:disabled kbd{border-color:#495745}
.transport-actions .soft{min-height:42px}
dialog{background:#1b201c;color:var(--text);border:1px solid #465344;border-radius:13px;padding:21px;width:min(430px,calc(100% - 32px));box-shadow:0 24px 90px #0009}
dialog::backdrop{background:#060907ab;backdrop-filter:blur(4px)}
.dialog-icon{display:grid;place-items:center;background:#353622;border:1px solid #565a39;color:var(--lime);border-radius:8px;width:34px;height:34px;margin-bottom:10px}
dialog h2{font-size:19px}
dialog p{color:#c0cabb;margin:8px 0 18px}
dialog form{display:flex;gap:7px;justify-content:flex-end}
dialog .primary{min-height:35px}
.toast{position:fixed;bottom:87px;right:clamp(14px,3vw,45px);max-width:min(440px,calc(100vw - 30px));background:#263122;color:#e9f5e4;border:1px solid #53694a;border-radius:9px;padding:11px 14px;font-size:12px;z-index:5;box-shadow:0 8px 32px #0006;animation:toast-in .18s ease-out}
.toast.error{background:#362626;border-color:#704749;color:#ffd9d9}
@keyframes toast-in{from{opacity:0;transform:translateY(4px)}}
@media(max-width:1160px){:root{--rail-width:186px}.hero-visual{width:49%}.layout{grid-template-columns:minmax(0,1fr) minmax(265px,292px)}.identity{padding:0 2px;gap:7px}.brand-icon{width:34px;height:34px}.brand-icon svg{width:20px;height:20px}.dropzone{gap:8px;padding:12px}.dropzone .browse svg{display:none}}
@media(max-width:970px){:root{--rail-width:64px}.rail{padding:17px 8px 12px}.identity{justify-content:center}.identity>div:last-child,.rail-label,.rail nav span,.rail nav b,.rail-exit span,.hardware{display:none!important}.rail-group{margin-top:27px}nav button,.rail-exit{justify-content:center;padding:0}.hero{height:188px}.hero-copy{padding-left:21px}}
@media(max-width:745px){.layout{grid-template-columns:minmax(0,1fr)}.hero{height:169px}.hero h1{font-size:43px}.hero p{max-width:78%;font-size:12px}.hero-meta{margin-top:12px}.hero-visual{opacity:.52;width:44%}.inspector{margin-bottom:5px}.top{min-height:48px}.main{padding-top:16px}.transport-inner{grid-template-columns:minmax(0,1fr) auto;gap:8px}.transport-status{grid-column:1}.progress{grid-row:2;grid-column:1/-1}.transport-actions{grid-row:1;grid-column:2}.transport{position:sticky}.toast{bottom:105px}}
@media(max-width:485px){:root{--rail-width:0px}.app{display:block}.rail{position:relative;height:auto;padding:5px 12px;flex-direction:row;align-items:center;border-right:0;border-bottom:1px solid var(--line);justify-content:space-between}.identity>div:last-child{display:block!important}.identity strong{font-size:13px}.identity span{font-size:7px}.brand-icon{width:29px;height:29px;border-radius:7px}.rail-group,.rail-bottom{margin:0}.rail nav{display:flex;gap:3px}.rail nav button,.rail-exit{width:31px;min-height:32px}.rail nav button[aria-current=page]::before{display:none}.top,.main,.transport-inner{padding-left:12px;padding-right:12px}.main{padding-bottom:14px}.hero{margin-bottom:13px}.hero h1{font-size:39px}.hero p{max-width:100%;font-size:11px}.hero-copy{padding-left:13px}.hero-visual{opacity:.29}.scope-label,.scope-scale{display:none}.card-head{min-height:64px;padding:10px 11px}.card-head p{font-size:10px}.card h2{font-size:14px}.dropzone{margin:0 11px;min-height:104px}.drop-title{font-size:12px}.drop-formats{font-size:8px}.drop-icon{width:30px;height:30px}.drop-icon svg{width:15px;height:15px}.dropzone .browse{font-size:10px;padding:0 7px;min-height:31px}.queue-toolbar{padding:0 10px}.queue-table th:nth-child(3){width:81px}.select-col{width:30px}.status{padding:2px 4px}.transport-icon,.primary kbd{display:none}.transport .button{min-height:34px;padding:0 7px;font-size:10px}.progress-label{margin-bottom:3px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important}.meter>div.indeterminate{animation:none!important;width:40%!important}}
</style>
</head>
<body>
<div class="app" id="app">
  <aside class="rail" aria-label="Application sidebar">
    <div class="identity"><div class="brand-icon" data-icon="wave"></div><div><strong>SRT Studio</strong><span>LOCAL AUDIO TOOLS</span></div></div>
    <div class="rail-group"><div class="rail-label">WORKSPACE</div>
      <nav id="nav" aria-label="Workspace">
        <button data-view="workbench" aria-current="page"><i data-icon="grid"></i><span>Workbench</span></button>
        <button data-view="exports"><i data-icon="export"></i><span>Exports</span><b id="nav-exports" hidden>0</b></button>
        <button data-view="activity"><i data-icon="pulse"></i><span>Activity</span></button>
      </nav>
    </div>
    <div class="rail-bottom">
      <section class="hardware" aria-label="Compute engine">
        <div class="hardware-top"><i data-icon="chip"></i><span>COMPUTE ENGINE</span><b class="led" id="engine-led"></b></div>
        <div id="engine-name" class="hardware-name">Initializing…</div>
        <div class="hardware-meta" id="engine-meta">Detecting hardware</div>
        <div class="meter" id="engine-meter" hidden><div id="engine-fill"></div></div>
        <div class="hardware-meta mono" id="engine-memory" hidden></div>
      </section>
      <button class="rail-exit" id="exit"><i data-icon="power"></i><span>Exit Studio</span></button>
    </div>
  </aside>
  <div class="shell">
    <header class="top">
      <div class="breadcrumb"><span>Studio</span><i data-icon="chevron-right"></i><strong id="crumb">Workbench</strong></div>
      <div class="local-pill"><span class="led"></span> LOCAL SESSION</div>
    </header>
    <main class="main">
      <section class="hero" id="hero" aria-labelledby="page-title">
        <div class="hero-copy"><div class="overline"><i data-icon="wave"></i> AUDIO <span class="mini-arrow">→</span> SUBTITLES</div>
          <h1 id="page-title">Transcription<span>.</span></h1>
          <p>Turn audio and video into subtitles, right on your machine.</p>
          <div class="hero-meta"><span><i data-icon="bolt"></i><b id="hero-model">TURBO</b></span><span class="meta-divider"></span><span><i data-icon="cpu"></i><b id="hero-compute">DETECTING DEVICE</b></span></div>
        </div>
        <div class="hero-visual" aria-hidden="true">
          <div class="reticle tl"></div><div class="reticle tr"></div><div class="reticle bl"></div><div class="reticle br"></div>
          <div class="scope-label">SIGNAL VIEW <span> // </span> WS–01</div>
          <svg id="wave-art" viewBox="0 0 650 201" preserveAspectRatio="none"></svg>
          <div class="scope-scale"><span>00:00</span><span>00:05</span><span>00:10</span><span>00:15</span></div>
        </div>
      </section>
      <div class="layout">
        <div class="workcol">
          <section class="card pane" id="view-workbench" aria-labelledby="queue-title">
            <header class="card-head">
              <div class="card-id"><div class="section-icon" data-icon="stack"></div><div><h2 id="queue-title">Media queue <span id="queue-count" class="count">00</span></h2><p>Drop files in the order you want them processed.</p></div></div>
              <button class="button soft" id="add-folder"><i data-icon="folder-plus"></i><span>Add folder</span></button>
            </header>
            <div id="dropzone" class="dropzone" aria-label="Drop audio or video files">
              <div class="drop-icon" data-icon="upload"></div>
              <div><div class="drop-title">Drop audio or video here</div><div class="drop-formats mono">WAV <b>·</b> MP3 <b>·</b> M4A <b>·</b> MP4 <b>·</b> + more</div></div>
              <button class="button browse" id="add-files"><i data-icon="plus"></i><span>Browse files</span></button>
              <button class="quiet browser-picker" id="browser-picker" hidden>Use browser picker</button>
              <input id="file-input" type="file" multiple hidden>
            </div>
            <div class="queue-toolbar" aria-label="Queue actions">
              <div class="queue-caption mono" id="selection-label">PROCESSING ORDER</div>
              <div class="row-actions"><button class="icon-button" id="move-up" title="Move selection up" aria-label="Move selection up" disabled><i data-icon="up"></i></button>
                <button class="icon-button" id="move-down" title="Move selection down" aria-label="Move selection down" disabled><i data-icon="down"></i></button>
                <button class="icon-button" id="remove" title="Remove selected" aria-label="Remove selected" disabled><i data-icon="trash"></i></button>
                <span></span><button class="quiet" id="clear" disabled>Clear queue</button></div>
            </div>
            <div class="table-shell">
              <table class="queue-table"><thead><tr><th class="select-col"><input id="select-all" type="checkbox" aria-label="Select all files" disabled></th><th scope="col">Media file</th><th scope="col">Status</th><th scope="col" class="row-open-head"><span class="sr-only">Open</span></th></tr></thead><tbody id="queue-body"></tbody></table>
              <div id="queue-empty" class="empty"><i data-icon="headphones"></i><div><strong>Your queue is empty</strong><span>Files you add will appear here.</span></div></div>
            </div>
          </section>
          <section class="card pane" id="view-exports" aria-labelledby="exports-title" hidden>
            <header class="card-head"><div class="card-id"><div class="section-icon" data-icon="export"></div><div><h2 id="exports-title">Exports <span id="exports-count" class="count">00</span></h2><p>Subtitle files from the current queue.</p></div></div>
              <button class="button soft" id="open-dest" disabled><i data-icon="folder"></i><span>Open destination</span></button>
            </header>
            <div id="export-list" class="export-list"></div>
          </section>
          <section class="card pane" id="view-activity" aria-labelledby="activity-title" hidden>
            <header class="card-head"><div class="card-id"><div class="section-icon" data-icon="pulse"></div><div><h2 id="activity-title">Activity</h2><p>Engine updates and batch results.</p></div></div>
              <button class="button soft" id="open-log"><i data-icon="external"></i><span>Open log</span></button></header>
            <div id="activity-list" class="activity-list" aria-label="Recent activity"></div>
          </section>
        </div>
        <section class="card inspector" id="inspector" aria-labelledby="settings-title">
          <header class="card-head inspector-head"><div class="card-id"><div class="section-icon" data-icon="sliders"></div><div><h2 id="settings-title">Configuration</h2><p>Settings for the next batch.</p></div></div>
          <button class="icon-button reset" id="reset" title="Reset configuration" aria-label="Reset configuration"><i data-icon="reset"></i></button></header>
          <div class="tabs" role="tablist" aria-label="Configuration sections">
            <button id="tab-speech" type="button" role="tab" aria-controls="panel-speech" aria-selected="true" data-tab="speech">Speech</button>
            <button id="tab-compute" type="button" role="tab" aria-controls="panel-compute" aria-selected="false" tabindex="-1" data-tab="compute">Compute</button>
            <button id="tab-output" type="button" role="tab" aria-controls="panel-output" aria-selected="false" tabindex="-1" data-tab="output">Output</button>
          </div>
          <fieldset id="config-fields">
            <div id="panel-speech" class="settings-panel" role="tabpanel" aria-labelledby="tab-speech">
              <label class="control"><span>Whisper model</span><select id="model"></select></label>
              <label class="control"><span>Spoken language</span><select id="language"></select></label>
              <div class="control"><span>Task</span><div class="segmented" aria-label="Speech task">
                <label><input type="radio" name="task" value="transcribe"><span><i data-icon="wave"></i>Transcribe</span></label>
                <label><input type="radio" name="task" value="translate"><span><i data-icon="globe"></i>Translate</span></label></div></div>
              <label class="switch"><span><strong>Prevent repetition</strong><small>Use each segment independently</small></span><input type="checkbox" id="no_loop"><i></i></label>
            </div>
            <div id="panel-compute" class="settings-panel" role="tabpanel" aria-labelledby="tab-compute" hidden>
              <label class="control"><span>Compute device</span><select id="device"></select></label>
              <div class="control two"><label><span>Precision</span><select id="precision"><option value="auto">Auto</option><option value="fp16">FP16</option><option value="fp32">FP32</option></select></label>
                <label><span>CPU threads</span><input id="cpu_threads" type="number" min="1" max="4096" step="1"></label></div>
              <label class="control"><span>Quality preset</span><select id="preset"><option>Fast</option><option>Balanced</option><option>Accurate</option></select></label>
              <label class="switch"><span><strong>Keep model loaded</strong><small>Reuse it for the next batch</small></span><input type="checkbox" id="keep_model_loaded"><i></i></label>
            </div>
            <div id="panel-output" class="settings-panel" role="tabpanel" aria-labelledby="tab-output" hidden>
              <div class="control"><span>Export formats</span><div class="segmented formats">
                <label><input id="fmt_srt" type="checkbox"><span>SRT</span></label>
                <label><input id="fmt_vtt" type="checkbox"><span>VTT</span></label>
                <label><input id="fmt_txt" type="checkbox"><span>TXT</span></label>
              </div></div>
              <label class="control"><span>Save location</span><select id="out_mode"><option>Next to source</option><option>Custom folder</option></select></label>
              <div class="path-row" id="path-row" hidden><input id="out_dir" aria-label="Output folder path" placeholder="Enter local folder path"><button class="icon-button" id="pick-output" aria-label="Browse for output folder" title="Browse for output folder"><i data-icon="folder"></i></button></div>
              <div class="drop-hint" id="drop-hint"></div>
              <label class="switch"><span><strong>Overwrite exports</strong><small>Replace matching output files</small></span><input type="checkbox" id="overwrite"><i></i></label>
              <label class="switch"><span><strong>Open folder when done</strong><small>Show the last export destination</small></span><input type="checkbox" id="open_when_done"><i></i></label>
            </div>
          </fieldset>
          <footer class="config-foot"><i data-icon="export"></i><span id="config-summary"></span></footer>
        </section>
      </div>
      <div class="inline-alert" id="engine-error" role="status" hidden></div>
    </main>
    <footer class="transport" aria-label="Batch controls">
      <div class="transport-inner">
        <div class="transport-status"><div class="transport-icon" id="transport-icon" data-icon="wave"></div><div class="transport-copy">
          <div id="batch-title" role="status">Ready when you are</div><div id="batch-sub">Add media to start a new batch.</div></div></div>
        <div class="progress" aria-label="Batch progress">
          <div class="progress-label mono"><span>BATCH PROGRESS</span><span id="batch-count">0 / 0</span></div>
          <div class="meter" role="progressbar" aria-label="Batch completion" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="batch-fill"></div></div>
        </div>
        <div class="transport-actions"><button class="button soft" id="stop" hidden><i data-icon="square"></i><span>Stop</span></button>
          <button class="button primary" id="start" disabled><i data-icon="play"></i><span>Start transcription</span><kbd title="Ctrl+Enter">↵</kbd></button></div>
      </div>
    </footer>
  </div>
</div>
<dialog id="confirm-dialog" aria-labelledby="dialog-title"><div class="dialog-icon" data-icon="alert"></div>
  <h2 id="dialog-title"></h2><p id="dialog-text"></p><form method="dialog"><button class="button soft" value="cancel" autofocus>Cancel</button><button class="button primary" id="dialog-action" value="ok">Continue</button></form>
</dialog>
<div id="toast" class="toast" role="status" hidden></div>
<script>"use strict";
const $ = (id) => document.getElementById(id);
const all = (q, el=document) => [...el.querySelectorAll(q)];
const art = {
  wave:'<path d="M2 12h3m2-4v8m3-12v16m4-14v12m3-10v8m2-4h3"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.1"/><rect x="14" y="3" width="7" height="7" rx="1.1"/><rect x="3" y="14" width="7" height="7" rx="1.1"/><rect x="14" y="14" width="7" height="7" rx="1.1"/>',
  export:'<path d="M7 17H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v2m-7 6h10m-4-4 4 4-4 4M7 21h11a2 2 0 0 0 2-2"/>',
  pulse:'<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  chip:'<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
  cpu:'<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9z"/>',
  power:'<path d="M12 2v10m5-7a8 8 0 1 1-10 0"/>',
  "chevron-right":'<path d="m9 5 7 7-7 7"/>',
  bolt:'<path d="m13 2-9 12h7l-1 8 10-12h-7z"/>',
  stack:'<path d="m12 3 9 5-9 5-9-5 9-5zm-9 9 9 5 9-5m-18 4 9 5 9-5"/>',
  "folder-plus":'<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zm9 4v6m-3-3h6"/>',
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  upload:'<path d="M12 15V3m-5 5 5-5 5 5M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  up:'<path d="M12 19V5m-5 5 5-5 5 5"/>',
  down:'<path d="M12 5v14m-5-5 5 5 5-5"/>',
  trash:'<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/>',
  headphones:'<path d="M3 14v-2a9 9 0 0 1 18 0v2M3 15a2 2 0 0 1 2-2h2v7H5a2 2 0 0 1-2-2zm18 0a2 2 0 0 0-2-2h-2v7h2a2 2 0 0 0 2-2z"/>',
  sliders:'<path d="M5 3v7m0 4v7m7-18v12m0 4v2m7-18v2m0 4v12M2 10h6m1 5h6m1-10h6"/>',
  reset:'<path d="M5 10a7 7 0 1 1 1 7m-2-7V4m0 6h6"/>',
  globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  external:'<path d="M13 4h7v7m-9 2 9-9m-3 9v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h5"/>',
  play:'<path d="m7 4 13 8-13 8z" fill="currentColor" stroke-linejoin="round"/>',
  square:'<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/>',
  audio:'<path d="M9 18V5l10-2v12M9 18a3 2 0 1 1-6 0 3 2 0 0 1 6 0zm10-3a3 2 0 1 1-6 0 3 2 0 0 1 6 0z"/>',
  video:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
  alert:'<path d="M12 8v4m0 4h.01M11 3 2 19a1 1 0 0 0 1 1h18a1 1 0 0 0 1-1L13 3a1.15 1.15 0 0 0-2 0z"/>'
};
function icon(key) {return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+art[key]+'</svg>';}
all('[data-icon]').forEach(el => { el.innerHTML=icon(el.dataset.icon); });
all('#nav button, #exit').forEach(el => {el.setAttribute('aria-label',el.querySelector('span').textContent); el.title=el.querySelector('span').textContent;});
function oscilloscope() {
  let bars='', line1='', line2='';
  const spikes=[[.15,.038,.25],[.26,.048,.61],[.355,.028,.37],[.445,.052,.83],[.575,.051,.63],[.663,.03,1.14],[.735,.04,.38],[.835,.037,.65],[.929,.028,.30]];
  for(let i=0;i<101;i++) {
    const p=i/100, x=24+i*6.1;
    const env=spikes.reduce((s,[c,w,a])=>s+a*Math.exp(-Math.pow((p-c)/w,2)),0);
    const rhythm=.18+.82*Math.abs(Math.sin(i*1.79+.5)*Math.cos(i*.79-.8));
    const a=3+68*env*rhythm;
    const y=100+38*env*Math.sin(i*.52)*Math.cos(i*.36);
    const z=100-28*env*Math.cos(i*.57+.2);
    bars+='<path d="M'+x.toFixed(1)+' '+(100-a).toFixed(1)+'v'+(a*2).toFixed(1)+'" opacity="'+(.25+.72*rhythm).toFixed(2)+'"/>';
    line1+=(i?'L':'M')+x.toFixed(2)+' '+y.toFixed(2);
    line2+=(i?'L':'M')+x.toFixed(2)+' '+z.toFixed(2);
  }
  $('wave-art').innerHTML='<defs><linearGradient id="signal"><stop offset="0%" stop-color="#74914c"/><stop offset="55%" stop-color="#d8f784"/><stop offset="100%" stop-color="#a2cb66"/></linearGradient></defs><g stroke="url(#signal)" stroke-width="2.25" stroke-linecap="round">'+bars+'</g><path d="'+line1+'" fill="none" stroke="#f3ffd0" stroke-opacity=".48" stroke-width="1.1"/><path d="'+line2+'" fill="none" stroke="#b9e47c" stroke-opacity=".37" stroke-width="1"/>';
}
oscilloscope();

const esc = (s) => String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const pad = (n) => String(n).padStart(2,'0');
const media = ['wav','mp3','m4a','flac','ogg','aac','wma','opus','mp4','mkv','mov','avi','webm','m4v'];
const videos = ['MP4','MKV','MOV','AVI','WEBM','M4V'];
const fields = ['model','language','device','precision','preset','cpu_threads','keep_model_loaded',
  'no_loop','fmt_srt','fmt_vtt','fmt_txt','out_mode','out_dir','overwrite','open_when_done'];
const token = location.hash.slice(1) || sessionStorage.getItem('ws-session') || '';
if(location.hash){sessionStorage.setItem('ws-session',token); history.replaceState(null,'',location.pathname);}
$('file-input').accept = media.map(x=>'.'+x).join(',');

let state=null, initial=true, exited=false, requestNo=0, renderedNo=0, since=0, priorRev=-1, fails=0, toastTimer=0;
let settingsTimer=0, settingsSerial=Promise.resolve(), selected=new Set(), picker=false, uploading=false, starting=false;
let currentView='workbench';
async function request(path, body, opts={}) {
  const headers={'X-Studio-Token':token,...opts.headers};
  if(body!==undefined && !opts.raw){headers['Content-Type']='application/json'; body=JSON.stringify(body);}
  const res=await fetch(path,{method:body===undefined?'GET':'POST',headers,body,cache:'no-store'});
  const data=await res.json();
  if(!res.ok) throw new Error(data.error || 'Studio could not complete that action.');
  return data;
}
function toast(message,error=false) {
  $('toast').textContent=message;
  $('toast').className='toast'+(error?' error':''); $('toast').hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>{$('toast').hidden=true;},error?6500:3800);
}
function question(title,message,action) {
  const diag=$('confirm-dialog');
  if(diag.open) return Promise.resolve(false);
  $('dialog-title').textContent=title; $('dialog-text').textContent=message;
  $('dialog-action').textContent=action; diag.returnValue='cancel'; diag.showModal();
  return new Promise(resolve=>diag.addEventListener('close',()=>resolve(diag.returnValue==='ok'),{once:true}));
}
function view(v) {
  currentView=v;
  ['workbench','exports','activity'].forEach(key=>$('view-'+key).hidden=key!==v);
  all('[data-view]').forEach(el=>el.dataset.view===v?el.setAttribute('aria-current','page'):el.removeAttribute('aria-current'));
  $('crumb').textContent=v[0].toUpperCase()+v.slice(1);
}
function tab(t,focus=false) {
  all('[data-tab]').forEach(el=>{
    const active=t===el.dataset.tab;
    el.setAttribute('aria-selected',String(active)); el.tabIndex=active?0:-1;
    $('panel-'+el.dataset.tab).hidden=!active;
    if(active && focus) el.focus();
  });
}
function replaceOptions(id, values, updateValue) {
  const select=$(id); const key=JSON.stringify(values);
  if(key===select.dataset.key) return;
  const old=select.value;
  select.replaceChildren(...values.map(([k,v])=>new Option(v,k)));
  select.dataset.key=key;
  if([...select.options].some(o=>o.value===old)) select.value=old;
  else select.value=updateValue;
}
function syncForm(cfg) {
  fields.forEach(k=>{const el=$(k); if(el.type==='checkbox') el.checked=cfg[k]; else el.value=cfg[k];});
  const task=all('[name=task]').find(el=>el.value===cfg.task); if(task) task.checked=true;
  refreshConfig();
}
function getConfig() {
  return Object.fromEntries([...fields.map(k=>[k,$(k).type==='checkbox'?$(k).checked:$(k).value]),
    ['task',document.querySelector('[name=task]:checked')?.value || 'transcribe']]);
}
function updateEngine() {
  if(!state) return;
  const engine=state.engine, chosen=$('device').value || 'auto', devices=state.devices;
  const device=devices.length && chosen!=='cpu' ? devices.find(d=>String(d.index)===chosen.split(':')[1]) || devices[0] : null;
  const loading=engine.state==='checking';
  $('engine-led').className='led '+(loading?'wait':engine.state==='error'?'error':'');
  $('engine-name').textContent=loading?'Initializing…':engine.state==='error' && !engine.torch?'Engine offline':device?device.name:'CPU processing';
  $('engine-meta').textContent=loading?'Detecting hardware':device?'CUDA '+(engine.cuda||'')+' · GPU '+device.index:engine.torch?'PyTorch '+engine.torch:'Whisper unavailable';
  $('hero-compute').textContent=loading?'DETECTING DEVICE':device?'CUDA · GPU '+device.index:engine.state==='error'?'ENGINE UNAVAILABLE':'CPU';
  const used=device && engine.vram_device===device.index ? engine.vram : null;
  $('engine-meter').hidden=$('engine-memory').hidden=!device;
  if(device) {
    $('engine-fill').style.width=(used===null?0:Math.min(100,100*used/device.total))+'%';
    $('engine-memory').textContent=(used===null?'—':used.toFixed(1))+' / '+device.total.toFixed(1)+' GB VRAM';
  }
}
function refreshConfig() {
  const c=getConfig();
  $('path-row').hidden=c.out_mode!=='Custom folder';
  $('hero-model').textContent=(c.model || 'WHISPER').toUpperCase();
  const formats=['srt','vtt','txt'].filter(x=>c['fmt_'+x]).map(x=>x.toUpperCase());
  $('config-summary').textContent=(formats.length?formats.join(' + '):'No format selected')+
    '  /  '+(c.out_mode==='Custom folder'?'CUSTOM FOLDER':'NEXT TO SOURCE');
  $('drop-hint').hidden=c.out_mode==='Custom folder';
  $('drop-hint').textContent=state?'Browser imports save to '+state.drop_folder:'';
  updateEngine();
}
function persistCfg(immediate=false) {
  clearTimeout(settingsTimer);
  const save=()=>{
    const cfg=getConfig();
    if(!cfg.cpu_threads || !state || state.busy) return;
    settingsSerial=settingsSerial.then(()=>request('/api/config',{config:cfg})).catch(err=>toast(err.message,true));
  };
  if(immediate) save(); else settingsTimer=setTimeout(save,220);
}
function selectionUI() {
  if(!state) return;
  const locked=state.busy || starting || uploading;
  $('select-all').checked=!!state.rows.length && selected.size===state.rows.length;
  $('select-all').indeterminate=!!selected.size && selected.size<state.rows.length;
  $('select-all').disabled=locked || !state.rows.length;
  $('selection-label').textContent=selected.size?pad(selected.size)+' SELECTED':'PROCESSING ORDER';
  all('#queue-body tr[data-id]').forEach(row=>{
    const box=row.querySelector('input');
    box.checked=selected.has(row.dataset.id); box.disabled=locked;
    row.classList.toggle('selected',box.checked);
  });
  ['move-up','move-down','remove'].forEach(k=>{$(k).disabled=locked || !selected.size;});
  $('clear').disabled=locked || !state.rows.length;
}
function fileSize(bytes) {
  return bytes>=2**30?(bytes/2**30).toFixed(1)+' GB':bytes>=2**20?(bytes/2**20).toFixed(1)+' MB':
    bytes>=1024?Math.round(bytes/1024)+' KB':bytes+' B';
}
function renderRows() {
  const rows=state.rows;
  selected=new Set([...selected].filter(id=>rows.some(r=>r.id===id)));
  $('queue-count').textContent=pad(rows.length);
  $('queue-empty').hidden=!!rows.length;
  $('queue-body').innerHTML=rows.map(r=>{
    const fileArt=videos.includes(r.ext)?'video':'audio';
    const detail=r.detail || [r.ext,fileSize(r.size),r.source==='Browser import'?'Browser import':''].filter(Boolean).join(' · ');
    const openable=r.outputs.length || r.source!=='Browser import';
    return '<tr data-id="'+esc(r.id)+'"><td class="select-col"><input type="checkbox" aria-label="Select '+esc(r.name)+
      '"></td><td><div class="file-cell"><div class="file-icon">'+icon(fileArt)+'</div><div class="file-text"><div class="file-name" title="'+esc(r.name)+'">'+esc(r.name)+
      '</div><div class="file-info" title="'+esc(r.detail || r.source)+'">'+esc(detail)+'</div></div></div></td><td><span class="status '+r.status[0].toLowerCase()+'">'+esc(r.status)+
      '</span></td><td>'+(openable?'<button class="icon-button row-open" title="Open '+(r.outputs.length?'output':'source')+'" aria-label="Open '+(r.outputs.length?'output':'source')+'">'+icon('external')+'</button>':'')+'</td></tr>';
  }).join('');
  const exports=rows.filter(x=>x.outputs.length);
  const count=exports.reduce((n,x)=>n+x.outputs.length,0);
  $('exports-count').textContent=pad(count);
  $('nav-exports').textContent=count; $('nav-exports').hidden=!count;
  $('export-list').innerHTML=exports.length?exports.map(r=>'<div class="export-row" data-id="'+esc(r.id)+'"><div class="file-text"><div class="file-name" title="'+esc(r.name)+'">'+esc(r.name)+
    '</div><div class="file-info" title="'+esc(r.outputs.map(x=>x.path).join(' · '))+'">'+esc(r.outputs[0].path)+'</div></div><div class="export-tags">'+r.outputs.map(x=>'<b>'+esc(x.format.toUpperCase())+'</b>').join('')+
    '</div><button class="icon-button" aria-label="Open output folder" title="Open output folder">'+icon('folder')+'</button></div>').join(''):
    '<div class="empty"><i>'+icon('export')+'</i><div><strong>No exports yet</strong><span>Completed exports will appear here.</span></div></div>';
}
function renderEvents(events) {
  for(const ev of events) {
    if(ev.id<=since) continue;
    const row=document.createElement('div'); row.className='log-row';
    row.innerHTML='<time>'+esc(ev.time)+'</time><i class="'+esc(ev.tone)+'"></i><span>'+esc(ev.message)+'</span>';
    $('activity-list').prepend(row); since=ev.id;
  }
  while($('activity-list').children.length>175) $('activity-list').lastElementChild.remove();
}
function renderControls() {
  if(!state) return;
  const b=state.batch, locked=state.busy || starting || uploading;
  const finished=['finished','cancelled'].includes(b.phase);
  const ready=state.engine.state==='ready';
  const progress=state.busy && b.file_progress!==null?Math.round(b.file_progress*100)+'%':'';
  $('hero').classList.toggle('running',state.busy && b.phase!=='cancelling');
  $('engine-error').hidden=state.engine.state!=='error';
  $('engine-error').textContent=state.engine.error;
  $('config-fields').disabled=locked;
  $('reset').disabled=locked || picker;
  $('start').disabled=locked || picker || !state.rows.length || !ready || exited;
  $('start').querySelector('span').textContent=state.busy?'Transcribing…':'Start transcription';
  $('stop').hidden=!state.busy; $('stop').disabled=b.phase==='cancelling';
  $('open-dest').disabled=!state.output_dir;
  $('open-log').disabled=!state.logging;
  $('add-files').disabled=$('add-folder').disabled=$('browser-picker').disabled=locked || picker || exited;
  $('batch-title').textContent=uploading?'Importing local media…':exited?'Studio closed':b.message;
  const summary=['done','skipped','failed','cancelled'].filter(k=>b[k]).map(k=>b[k]+' '+k).join(' · ');
  $('batch-sub').textContent=b.file && state.busy?[b.file,progress,b.eta?'ETA '+b.eta:''].filter(Boolean).join(' · '):
     finished?summary:state.rows.length?state.rows.length+' file'+(state.rows.length===1?'':'s')+' in the queue.':'Add media to start a new batch.';
  $('batch-sub').title=$('batch-sub').textContent;
  $('batch-count').textContent=b.total?b.completed+' / '+b.total:'0 / 0';
  const frac=state.busy?Math.min(.999,(b.completed+(b.index>b.completed?b.file_progress||0:0))/Math.max(1,b.total)):
    finished && b.phase!=='cancelled'?1:finished ? (b.done+b.failed+b.skipped)/Math.max(1,b.total):0;
  $('batch-fill').style.width=(frac*100)+'%';
  const meter=$('batch-fill').parentElement;
  meter.setAttribute('aria-valuenow',Math.round(frac*100));
  meter.setAttribute('aria-valuetext',b.completed+' of '+b.total+' files processed');
  $('batch-fill').classList.toggle('indeterminate',state.busy && (b.phase==='loading'||(b.total===1 && b.phase==='running' && b.file_progress===null)));
  selectionUI();
}
async function refresh() {
  if(exited) return;
  const id=++requestNo;
  try{
    const data=await request('/api/state?since='+since);
    if(exited || id<renderedNo) return;
    renderedNo=id; fails=0; state=data;
    replaceOptions('model',data.models.map(m=>[m,m==='turbo'?'Turbo':m]),data.config.model);
    replaceOptions('language',data.languages.map(l=>[l,l]),data.config.language);
    replaceOptions('device',[['auto','Auto (prefer CUDA)'],['cpu','CPU'],
      ...data.devices.map(d=>['cuda:'+d.index,'GPU '+d.index+' · '+d.name]),
      ...(data.devices.length?[['cuda','Default CUDA GPU']]:[])],data.config.device);
    if(initial){syncForm(data.config); initial=false;}
    refreshConfig();
    if(data.rev!==priorRev){renderRows(); priorRev=data.rev;}
    renderEvents(data.events); renderControls();
  } catch(err) {
    if(exited) return;
    fails++;
    if(fails>=2 || !state) {
      $('engine-error').textContent=token?'Connection to Studio lost. If it was restarted, open its new local URL.':'Open Studio using the link printed in the terminal.';
      $('engine-error').hidden=false; $('start').disabled=true;
    }
  }
}
async function pickerAction(mode) {
  if(picker || !state || state.busy || uploading || starting) return;
  picker=true; renderControls();
  try{
    const data=await request('/api/picker',{mode});
    if(data.dir){$('out_dir').value=data.dir; $('out_mode').value='Custom folder'; refreshConfig(); persistCfg(true);}
    if(data.added!==undefined) toast(data.added?'Added '+data.added+' to the queue.':'No new supported files found.');
  }catch(err){
    if(mode!=='output') $('browser-picker').hidden=false;
    toast(mode==='output'?'Native picker unavailable. Enter the output path directly.':err.message,true);
  }finally{picker=false; await refresh(); renderControls();}
}
async function queueAction(action) {
  if(action==='clear' && !await question('Clear the media queue?','This removes the queued items from Studio. Your media and exports stay on disk.','Clear queue')) return;
  try{await request('/api/queue',{action,ids:[...selected]}); if(['clear','remove'].includes(action)) selected.clear(); await refresh();}
  catch(err){toast(err.message,true);}
}
async function start() {
  if($('start').disabled) return;
  const cfg=getConfig(); clearTimeout(settingsTimer);
  if(!cfg.cpu_threads || !$('cpu_threads').validity.valid) {tab('compute'); $('cpu_threads').reportValidity(); $('cpu_threads').focus(); return;}
  if(!cfg.fmt_srt && !cfg.fmt_vtt && !cfg.fmt_txt) {tab('output'); toast('Select at least one export format.',true); return;}
  if(cfg.out_mode==='Custom folder' && !cfg.out_dir.trim()) {tab('output'); $('out_dir').focus(); toast('Choose an output folder.',true); return;}
  starting=true; renderControls();
  try{
    await settingsSerial;
    const data=await request('/api/start',{config:cfg});
    if(data.confirm && await question('GPU memory headroom',data.confirm,'Start anyway')) {
      await request('/api/start',{config:cfg,allow_low_vram:true});
    }
    await refresh();
  }catch(err){toast(err.message,true);}
  finally{starting=false; renderControls();}
}
async function openTarget(kind,id) {
  try{await request('/api/open',{kind,id});}catch(err){toast(err.message,true);}
}
async function uploadFiles(files) {
  if(!state || state.busy || starting || uploading || exited) {toast('Stop the batch before importing.',true); return;}
  const supported=files.filter(f=>media.includes(f.name.split('.').pop().toLowerCase()));
  if(!supported.length){toast('No supported audio or video files found.',true); return;}
  uploading=true; renderControls();
  let added=0, problem='';
  try{
    for(const file of supported){
      try{await request('/api/upload',file,{raw:true,headers:{'Content-Type':'application/octet-stream','X-Media-Name':encodeURIComponent(file.name)}}); added++;}
      catch(err){problem=err.message;}
      if(added%4===0) await refresh();
    }
  }finally{uploading=false; await refresh(); renderControls();}
  if(added) toast('Imported '+added+' local file'+(added===1?'':'s')+'.');
  if(problem) toast((added?'Imported '+added+'. ':'')+problem,true);
}
async function walk(entry) {
  if(entry.isFile) return new Promise((resolve,reject)=>entry.file(x=>resolve([x]),reject));
  if(!entry.isDirectory) return [];
  const reader=entry.createReader(), children=[];
  while(true){
    const chunk=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));
    if(!chunk.length) break;
    children.push(...chunk);
  }
  children.sort((a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true}));
  const out=[];
  for(const child of children) out.push(...await walk(child));
  return out;
}
const drop=$('dropzone');
let dragDepth=0;
drop.addEventListener('dragenter',e=>{
  if(!e.dataTransfer.types.includes('Files')) return;
  e.preventDefault(); dragDepth++; drop.classList.add('dragging');
});
drop.addEventListener('dragleave',()=>{if(--dragDepth<=0){dragDepth=0; drop.classList.remove('dragging');}});
window.addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();e.dataTransfer.dropEffect=drop.contains(e.target)?'copy':'none';}});
window.addEventListener('drop',async e=>{
  if(!e.dataTransfer.types.includes('Files')) return;
  e.preventDefault(); dragDepth=0; drop.classList.remove('dragging');
  if(!drop.contains(e.target)) return;
  const direct=[...e.dataTransfer.files];
  const entries=[...e.dataTransfer.items].filter(x=>x.kind==='file').map(x=>x.webkitGetAsEntry?.()).filter(Boolean);
  try {
    const files=[];
    if(entries.length===direct.length && entries.some(x=>x.isDirectory)){
      for(const entry of entries) files.push(...await walk(entry));
    }
    await uploadFiles(files.length?files:direct);
  }catch(err){toast(err.message || 'Could not read these files.',true);}
});
$('file-input').addEventListener('change',e=>{const files=[...e.target.files]; e.target.value=''; if(files.length) uploadFiles(files);});
$('browser-picker').onclick=()=>$('file-input').click();
$('add-files').onclick=()=>pickerAction('files');
$('add-folder').onclick=()=>pickerAction('folder');
$('pick-output').onclick=()=>pickerAction('output');
$('open-log').onclick=()=>openTarget('log');
$('open-dest').onclick=()=>openTarget('destination');
$('start').onclick=start;
$('stop').onclick=async()=>{try{await request('/api/cancel',{});await refresh();}catch(err){toast(err.message,true);}};
$('reset').onclick=async()=>{
  clearTimeout(settingsTimer);
  try{await settingsSerial; const result=await request('/api/reset',{}); syncForm(result.config); toast('Default settings restored.');}
  catch(err){toast(err.message,true);}
};
$('clear').onclick=()=>queueAction('clear'); $('remove').onclick=()=>queueAction('remove');
$('move-up').onclick=()=>queueAction('up'); $('move-down').onclick=()=>queueAction('down');
$('select-all').onchange=e=>{
  selected=e.target.checked?new Set(state.rows.map(r=>r.id)):new Set();
  selectionUI();
};
$('queue-body').addEventListener('change',e=>{
  if(e.target.type!=='checkbox') return;
  const id=e.target.closest('tr').dataset.id;
  if(e.target.checked) selected.add(id); else selected.delete(id); selectionUI();
});
$('queue-body').addEventListener('click',e=>{if(e.target.closest('.row-open')) openTarget('file',e.target.closest('tr').dataset.id);});
$('queue-body').addEventListener('dblclick',e=>{if(!e.target.closest('button,input') && e.target.closest('tr')) openTarget('file',e.target.closest('tr').dataset.id);});
$('export-list').addEventListener('click',e=>{if(e.target.closest('button')) openTarget('folder',e.target.closest('.export-row').dataset.id);});
$('config-fields').addEventListener('change',()=>{refreshConfig();persistCfg();});
all('[data-view]').forEach(el=>{el.onclick=()=>view(el.dataset.view);});
all('[data-tab]').forEach(el=>{
  el.onclick=()=>tab(el.dataset.tab);
  el.addEventListener('keydown',e=>{
    const tabs=all('[data-tab]'), idx=tabs.indexOf(el);
    const target={ArrowLeft:(idx+2)%3,ArrowRight:(idx+1)%3,Home:0,End:2}[e.key];
    if(target!==undefined){e.preventDefault();tab(tabs[target].dataset.tab,true);}
  });
});
$('exit').onclick=async()=>{
  const running=state?.busy;
  if(!await question('Exit SRT Studio?',running?'The running batch will stop. Exported files are kept.':'The local server will shut down. Run the script again to reopen Studio.', 'Exit Studio'))return;
  clearTimeout(settingsTimer);
  try{
    if(!running && state && $('cpu_threads').validity.valid && $('cpu_threads').value) {
      await settingsSerial;
      await request('/api/config',{config:getConfig()});
    }
    await request('/api/shutdown',{});
    exited=true;
    document.body.innerHTML='<main class="main"><section class="card" style="padding:24px;max-width:500px;margin:48px auto"><div class="section-icon">'+icon('power')+'</div><h1 style="margin:12px 0 4px">Studio closed.</h1><p style="color:var(--muted)">You can close this tab.</p></section></main>';
  }catch(err){toast(err.message,true);}
};
document.addEventListener('keydown',e=>{
  if(!(e.ctrlKey || e.metaKey) || $('confirm-dialog')?.open || exited) return;
  if(e.key==='Enter'){e.preventDefault();start();}
  if(e.key.toLowerCase()==='o'){e.preventDefault();view('workbench');pickerAction('files');}
});
async function loop(){if(exited)return; await refresh(); if(!exited)setTimeout(loop,state?.busy?360:1300);}
loop();
</script>
</body>
</html>
'''


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, studio):
        self.studio = studio
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)
        self.authority = f"127.0.0.1:{self.server_address[1]}"

    def close_studio(self):
        self.studio.stop.set()
        self.studio.cancel.set()
        self.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "SRTStudio"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(180)

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)

    def respond(self, value, code=200, content_type="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def check_origin(self, api=True):
        if self.headers.get("Host") != self.server.authority:
            raise InputError("Open Studio using its local URL.", 403)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.server.authority}":
            raise InputError("Cross-origin requests are not allowed.", 403)
        if api and not hmac.compare_digest(self.headers.get("X-Studio-Token", "").encode("utf-8"),
                                           self.server.token.encode("utf-8")):
            raise InputError("Open Studio using its current launch link.", 403)

    def length(self):
        try:
            value = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise InputError("Invalid body size.") from None
        if value < 0:
            raise InputError("A body length is required.", 411)
        return value

    def read_json(self):
        size = self.length()
        if size > 131_072:
            raise InputError("The request is too large.", 413)
        try:
            data = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            raise InputError("Invalid request data.") from None
        if not isinstance(data, dict):
            raise InputError("Invalid request data.")
        return data

    def error(self, exc):
        if isinstance(exc, InputError):
            self.respond({"error": str(exc)}, exc.code)
        elif isinstance(exc, (OSError, ValueError)):
            LOG.debug("Could not complete request", exc_info=True)
            self.respond({"error": str(exc)}, 400)
        else:
            LOG.exception("Unexpected Studio error")
            self.respond({"error": "Studio could not complete that operation."}, 500)

    def do_GET(self):
        try:
            path = urlsplit(self.path)
            self.check_origin(path.path != "/")
            if path.path == "/":
                self.respond(PAGE.encode("utf-8"), content_type="text/html; charset=utf-8")
            elif path.path == "/api/state":
                since = path.query.partition("=")[2]
                self.respond(self.server.studio.snapshot(int(since) if since.isdigit() and len(since) < 12 else 0))
            else:
                raise InputError("Unknown route.", 404)
        except Exception as exc:
            self.error(exc)

    def do_POST(self):
        try:
            self.check_origin()
            studio = self.server.studio
            path = urlsplit(self.path).path.removeprefix("/api/")
            if path == "upload":
                if self.headers.get_content_type() != "application/octet-stream":
                    raise InputError("Invalid media upload.", 415)
                size = self.length()
                if size > 32 * 1024**3:
                    raise InputError("Files over 32 GB must be added with the native picker.", 413)
                name = unquote(self.headers.get("X-Media-Name", "")).replace("\\", "/").split("/")[-1]
                # Browser filenames can come from a different operating system than this process.
                if os.name == "nt":
                    name = "".join("_" if c in '<>:"|?*' or ord(c) < 32 else c for c in name)
                name = name.replace("\x00", "_")
                self.respond(studio.upload(name, size, self.rfile))
                return
            data = self.read_json()
            result = {}
            if path == "config":
                with studio.lock:
                    studio.require_idle()
                    studio.save(studio.validate(data.get("config")))
            elif path == "reset":
                with studio.lock:
                    studio.require_idle()
                    defaults = dict(DEFAULTS)
                    if defaults["model"] not in studio.models:
                        defaults["model"] = "medium" if "medium" in studio.models else studio.models[0]
                    studio.save(defaults)
                    result = {"config": defaults}
            elif path == "picker":
                result = studio.picker(data.get("mode"))
            elif path == "queue":
                studio.edit_queue(data.get("action"), data.get("ids", []))
            elif path == "open":
                studio.open_target(data)
            elif path == "start":
                result = studio.start(data)
            elif path == "cancel":
                studio.cancel_run()
            elif path != "shutdown":
                raise InputError("Unknown route.", 404)
            self.respond(result)
            if path == "shutdown":
                threading.Thread(target=self.server.close_studio, daemon=True).start()
        except Exception as exc:
            self.error(exc)


def main():
    parser = argparse.ArgumentParser(description="Run the local SRT Studio workbench.")
    parser.add_argument("--no-browser", action="store_true", help="Print the local URL without opening it.")
    parser.add_argument("--port", default=0, type=int, help="Local port. The default picks a free port.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        logging.basicConfig(filename=LOG_FILE, encoding="utf-8", level=logging.INFO,
                            format="%(asctime)s | %(levelname)s | %(message)s")
    except OSError:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    studio = Studio()
    try:
        server = LocalServer(("127.0.0.1", args.port), studio)
    except OSError as exc:
        parser.exit(1, f"Could not start SRT Studio: {exc}\n")
    url = f"http://{server.authority}/#{server.token}"
    print(f"{NAME}\n{url}\nExit from Studio or press Ctrl+C here.\n", flush=True)
    if not args.no_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        with server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        studio.stop.set()
        studio.cancel.set()
        if studio.worker and studio.worker.is_alive():
            studio.worker.join(timeout=1)
        try:
            studio.cache.cleanup()
        except OSError:
            pass


if __name__ == "__main__":
    main()

"""Download and validate a desktop Whisper model before going offline."""

import argparse
import json
import os
from pathlib import Path

from srt_studio import SETTINGS, atomic_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", nargs="?", default="turbo")
    parser.add_argument("--select", action="store_true", help="Use this model as the desktop default.")
    args = parser.parse_args()

    import whisper

    if args.model not in whisper.available_models():
        parser.error("unknown model; choose from " + ", ".join(whisper.available_models()))
    configured = os.environ.get("SRT_STUDIO_MODEL_DIR")
    standard = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "whisper"
    target = Path(configured).expanduser().resolve() if configured else standard
    target.mkdir(parents=True, exist_ok=True)
    print(f"Preparing {args.model} in {target}", flush=True)
    whisper.load_model(args.model, device="cpu", download_root=str(target))
    if args.select:
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except FileNotFoundError:
            settings = {}
        except (OSError, ValueError) as exc:
            parser.error(f"model is cached, but settings could not be read: {exc}")
        if not isinstance(settings, dict):
            parser.error("model is cached, but the settings file is not a JSON object")
        settings["model"] = args.model
        atomic_write(SETTINGS, json.dumps(settings, indent=2) + "\n")
    print("Model verified and ready for offline use.")


if __name__ == "__main__":
    main()

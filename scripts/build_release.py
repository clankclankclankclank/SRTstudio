"""Assemble a reproducible Windows distribution and SHA-256 verification file."""
import argparse
import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "packaging" / "windows"
FILES = {
    "Install-SRT-Studio.cmd": WINDOWS / "Install-SRT-Studio.cmd",
    "Prepare-Offline.cmd": WINDOWS / "Prepare-Offline.cmd",
    "README.txt": WINDOWS / "README.txt",
    "Start-SRT-Studio.cmd": WINDOWS / "Start-SRT-Studio.cmd",
    "prepare_offline.py": ROOT / "prepare_offline.py",
    "requirements.txt": ROOT / "requirements.txt",
    "srt_studio.py": ROOT / "srt_studio.py",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "downloads")
    args = parser.parse_args()
    tag = (ROOT / "release" / "VERSION").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        parser.error("release/VERSION must have the form v0.0.0")
    root = "SRT-Studio-" + tag[1:] + "-Windows"
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / (root + ".zip")
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=9) as out:
        for name, source in FILES.items():
            info = ZipInfo(root + "/" + name, (2026, 11, 19, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            data = source.read_bytes()
            if name.endswith((".cmd", ".txt")):
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            out.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    print(f"{path} ({path.stat().st_size:,} bytes)\n{digest}")


if __name__ == "__main__":
    main()

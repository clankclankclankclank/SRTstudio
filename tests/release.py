"""Check the Windows archive and offline preparation without downloading a model."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "docs" / "downloads"
ZIP = DIST / "SRT-Studio-0.1.0-Windows.zip"


class ReleaseTest(unittest.TestCase):
    def test_download_package(self):
        recorded, name = ZIP.with_suffix(".sha256").read_text().split()
        self.assertEqual(name, ZIP.name)
        self.assertEqual(hashlib.sha256(ZIP.read_bytes()).hexdigest(), recorded)
        with ZipFile(ZIP) as archive:
            self.assertIsNone(archive.testzip())
            names = {Path(p).name for p in archive.namelist()}
            self.assertEqual(names, {"Install-SRT-Studio.cmd", "Prepare-Offline.cmd",
                                     "Start-SRT-Studio.cmd", "README.txt", "prepare_offline.py",
                                     "requirements.txt", "srt_studio.py"})
            for p in archive.namelist():
                if p.endswith(".cmd"):
                    self.assertIn(b"\r\n", archive.read(p))
            self.assertNotIn("models", names)

    def test_offline_preparation_and_default(self):
        with tempfile.TemporaryDirectory() as temp:
            place = Path(temp)
            for name in ("srt_studio.py", "prepare_offline.py"):
                shutil.copy(ROOT / name, place / name)
            settings = place / "settings_whisper_gpu.json"
            settings.write_text('{"language": "English", "model": "turbo"}')
            (place / "whisper.py").write_text(
                "from pathlib import Path\n"
                "def available_models(): return ['small', 'turbo']\n"
                "def load_model(name, *, device, download_root):\n"
                "    assert device == 'cpu'\n"
                "    (Path(download_root) / (name + '.pt')).write_text('cached')\n"
            )
            models = place / "models"
            env = {**os.environ, "SRT_STUDIO_MODEL_DIR": str(models)}
            result = subprocess.run([sys.executable, str(place / "prepare_offline.py"), "small", "--select"],
                                    env=env, text=True, check=True, capture_output=True)
            self.assertIn("ready for offline use", result.stdout)
            self.assertTrue((models / "small.pt").exists())
            self.assertEqual(json.loads(settings.read_text()), {"language": "English", "model": "small"})


if __name__ == "__main__":
    unittest.main()

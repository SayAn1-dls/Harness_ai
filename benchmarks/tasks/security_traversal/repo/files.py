import os
from pathlib import Path

UPLOAD_DIR = Path(__file__).parent / "uploads"


def read_upload(name):
    with open(os.path.join(UPLOAD_DIR, name), encoding="utf-8") as fh:
        return fh.read()

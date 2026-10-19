"""Общее для тестов студии: пути проекта."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
CLIENT = ROOT / "client"
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(APP))             # image_upload — рядом с server.py

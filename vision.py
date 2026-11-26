"""Распознавание состояния игры по снимку окна: в рейде ли персонаж, появились ли нужные кнопки."""

from pathlib import Path

import cv2
import numpy as np

TEMPLATES = Path(__file__).resolve().parent / "templates"
BASE_W = 1920  # все шаблоны хранятся в масштабе окна шириной 1920


def normalize(img_rgb):
    """Приводит снимок к ширине 1920, чтобы шаблоны подходили при любом размере окна."""
    h, w = img_rgb.shape[:2]
    if w == BASE_W:
        return img_rgb
    return cv2.resize(img_rgb, (BASE_W, round(h * BASE_W / w)), interpolation=cv2.INTER_AREA)

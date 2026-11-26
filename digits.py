"""Чтение чисел из интерфейса игры (количество наград «x360», «x5» и т.п.).

Шрифт в игре один, высота цифр при окне шириной 1920 — около 13 px. Шаблоны цифр лежат в
templates/digits/<символ>.png (вырезаны из скриншотов, см. build_templates()). Текст отделяется
от фона по яркости, режется на символы по пустым столбцам, каждый символ сравнивается с шаблонами.
"""

from pathlib import Path

import cv2
import numpy as np

DIR = Path(__file__).resolve().parent / "templates" / "digits"
NORM = (12, 16)      # к какому размеру приводится символ перед сравнением (w, h)
_tpl = None


def text_mask(crop, frac=0.35):
    """Маска заливки текста: пиксели заметно ярче фона.

    Фон — медиана участка, порог — frac пути от фона до самой яркой заливки: белый текст режется
    высоко (соседние цифры не слипаются), тусклый красный — низко (цифры не рассыпаются)."""
    v = crop.max(axis=2).astype(int)
    bg = np.median(v)
    return (v > bg + max(12, frac * (np.percentile(v, 99) - bg))).astype(np.uint8)


def segment(mask, min_h_frac=0.6):
    """Символы маски слева направо: список (x0, x1, y0, y1). Запятые/точки (низкие) отбрасываются."""
    cols = mask.any(axis=0)
    boxes, x = [], 0
    w = mask.shape[1]
    while x < w:
        if not cols[x]:
            x += 1
            continue
        x0 = x
        while x < w and cols[x]:
            x += 1
        rows = np.where(mask[:, x0:x].any(axis=1))[0]
        if mask[:, x0:x].sum() >= 4:
            boxes.append((x0, x, rows[0], rows[-1] + 1))
    if not boxes:
        return []
    hmax = max(b[3] - b[2] for b in boxes)
    return [b for b in boxes if b[3] - b[2] >= hmax * min_h_frac]


def _norm(glyph):
    g = cv2.resize(glyph.astype(np.float32), NORM, interpolation=cv2.INTER_AREA)
    g -= g.mean()
    n = np.linalg.norm(g)
    return g / n if n else g


def load_templates():
    global _tpl
    if _tpl is None:
        _tpl = {}
        for p in DIR.glob("*.png"):
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            label = p.stem.split("_")[0]
            _tpl.setdefault(label, []).append(_norm(img > 127))
    return _tpl


def classify(glyph):
    """(символ, сходство) для маски одного символа."""
    g = _norm(glyph)
    best, score = "?", -1.0
    for label, items in load_templates().items():
        for t in items:
            s = float((g * t).sum())
            if s > score:
                best, score = label, s
    return best, score


def read_text(crop, min_score=0.5):
    """Строка символов из участка экрана; неуверенные символы — «?»."""
    m = text_mask(crop)
    out = []
    for x0, x1, y0, y1 in segment(m):
        ch, s = classify(m[y0:y1, x0:x1])
        out.append(ch if s >= min_score else "?")
    return "".join(out)


def read_number(crop):
    """Целое число из участка («x360» → 360) или None, если цифр нет или какая-то не распознана."""
    s = read_text(crop)
    if s.startswith("x"):
        s = s[1:]
    if not s or not s.isdigit():
        return None
    return int(s)


# ---- сборка шаблонов из скриншотов (запускается вручную: python digits.py build <снимки…>)

# (файл-снимок, участок x0 y0 x1 y1 при ширине 1920, текст в нём без запятых/точек)
SOURCES = [
    ("m2.png", (195, 232, 320, 254), "124569383"),        # Sorcerer Strength
    ("m4.png", (960, 555, 1112, 582), "14202067098"),     # Damage Dealt 1,420,206,709.8
    ("m5.png", (1110, 592, 1208, 616), "Y264000"),        # ¥264,000
    ("m5.png", (846, 592, 944, 616), "x5"),               # сундуки
    ("m5.png", (978, 592, 1076, 616), "x13440000"),       # опыт
]


def build_templates(snap_dir):
    DIR.mkdir(parents=True, exist_ok=True)
    counts = {}
    for fname, (x0, y0, x1, y1), text in SOURCES:
        img = cv2.imread(str(Path(snap_dir) / fname))
        m = text_mask(img[y0:y1, x0:x1])
        boxes = segment(m)
        if len(boxes) != len(text):
            print(f"{fname} {text}: найдено {len(boxes)} символов вместо {len(text)}, пропускаю")
            continue
        for ch, (bx0, bx1, by0, by1) in zip(text, boxes):
            i = counts.get(ch, 0)
            if i >= 3:
                continue
            counts[ch] = i + 1
            cv2.imwrite(str(DIR / f"{ch}_{i}.png"), m[by0:by1, bx0:bx1] * 255)
    print("шаблоны:", dict(sorted(counts.items())))


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "build":
        build_templates(sys.argv[2])
    else:
        print("python digits.py build snaps/mahito")

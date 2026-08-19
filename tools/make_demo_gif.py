"""Regenerate assets/lucen.gif, the terminal demo shown in the README.

The recording is synthesized rather than captured so the asset is reproducible
and stays ASCII-only. The timings it displays are real: medians of three runs
of examples/scored_records.py under `python` and under `lucen run` on the
benchmark machine (12-core i5-12450HX, Windows 11, CPython 3.11).

    python tools/make_demo_gif.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 900, 288
MARGIN_X, MARGIN_Y = 24, 22
LINE_HEIGHT = 26
FRAME_MS = 40

BACKGROUND = (0, 0, 0)
PROMPT = (110, 120, 240)
COMMAND = (235, 235, 235)
OUTPUT = (170, 170, 170)
ACCENT = (110, 200, 140)

# One monospace face has to exist for the glyph grid to line up; the candidates
# cover Windows, macOS and the usual Linux font packages in that order.
FONT_CANDIDATES = (
    "C:/Windows/Fonts/consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
)

TYPE_FRAMES_PER_CHAR = 2
FRAMES_AFTER_COMMAND = 8
FRAMES_PER_OUTPUT_LINE = 7
FRAMES_AT_END = 55

# (command, output lines) pairs, played in order.
SCENES = (
    ("python scored_records.py", ("checksum: -660.436511", "elapsed: 5.73 s")),
    ("lucen run scored_records.py", ("checksum: -660.436511", "elapsed: 2.25 s")),
)
CLOSING = "same output, 2.5x faster on 12 cores"


def _load_font() -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, 18)
    raise SystemExit("no monospace font found; add one to FONT_CANDIDATES")


def _render(font: ImageFont.FreeTypeFont, lines, cursor: bool) -> Image.Image:
    """Draw the whole scrollback. `lines` are (text, color, is_prompt) tuples."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    y = MARGIN_Y
    for text, color, is_prompt in lines:
        x = MARGIN_X
        if is_prompt:
            draw.text((x, y), "$", font=font, fill=PROMPT)
            x += font.getlength("$ ")
        draw.text((x, y), text, font=font, fill=color)
        y += LINE_HEIGHT
    if cursor and lines:
        last_text, _, last_is_prompt = lines[-1]
        x = MARGIN_X + font.getlength("$ " if last_is_prompt else "") + font.getlength(last_text)
        draw.rectangle(
            (x + 2, y - LINE_HEIGHT + 3, x + 10, y - LINE_HEIGHT + 19), fill=COMMAND
        )
    return image


def build_frames(font: ImageFont.FreeTypeFont):
    frames = []
    lines = []

    def emit(count: int, cursor: bool = True) -> None:
        frames.extend(_render(font, lines, cursor) for _ in range(count))

    for index, (command, outputs) in enumerate(SCENES):
        if index:
            lines.append(("", OUTPUT, False))
        lines.append(("", COMMAND, True))
        for typed in range(1, len(command) + 1):
            lines[-1] = (command[:typed], COMMAND, True)
            emit(TYPE_FRAMES_PER_CHAR)
        emit(FRAMES_AFTER_COMMAND)
        for line in outputs:
            lines.append((line, OUTPUT, False))
            emit(FRAMES_PER_OUTPUT_LINE, cursor=False)

    lines.append(("", OUTPUT, False))
    lines.append((CLOSING, ACCENT, False))
    emit(FRAMES_AT_END, cursor=False)
    return frames


def main() -> int:
    frames = build_frames(_load_font())
    # A tiny palette is what keeps the asset small; the render uses five colors.
    quantized = [frame.quantize(colors=16, method=Image.MEDIANCUT) for frame in frames]
    target = Path(__file__).resolve().parent.parent / "assets" / "lucen.gif"
    quantized[0].save(
        target,
        save_all=True,
        append_images=quantized[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=True,
        disposal=1,
    )
    print(f"{target}: {len(quantized)} frames, {target.stat().st_size // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

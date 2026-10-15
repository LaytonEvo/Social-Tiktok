"""Assemble a finished video: clips, voiceover, style D overlays and the Clubhouse end card.

The look is ``docs/VIDEO_STYLE.md``: 1080x1920, a soft dark fade, a yellow
letter-spaced label over an Inter ExtraBold headline, the logo top left, and a
Clubhouse-green end card with the seal. Text is kept inside TikTok's safe area;
anything that won't fit at the smallest allowed size is an error, never drawn
over TikTok's buttons.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
X = 70                         # left margin
SAFE_RIGHT = 900               # TikTok's like/share buttons sit to the right of this
KICKER_Y, HEAD_Y = 1300, 1348  # above TikTok's caption area (y > 1520)
HEAD_SIZES = (82, 76, 70, 64, 58)
TEXT_BOTTOM, LINE_GAP = 1520, 1.12
YELLOW, WHITE, GREEN = (245, 219, 0), (255, 255, 255), (1, 103, 71)
END_CARD_SECONDS = 3.1
ASSETS = ROOT / "assets"
FONT_CACHE = Path.home() / ".cache" / "evo_tiktok" / "fonts"
INTER_CSS = "https://fonts.googleapis.com/css2?family=Inter:wght@500;700;800"


class RenderError(RuntimeError):
    pass


@dataclass
class Segment:
    clip: Path          # source clip
    start: float        # seconds into the clip
    duration: float     # seconds on screen
    speed: float = 1.0  # < 1 slows the clip down to fill the time
    kicker: str = ""
    headline: str = ""


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RenderError("ffmpeg not found: install imageio-ffmpeg")
        return exe


def probe(path: Path) -> dict:
    """Duration (s), width and height, read from ffmpeg's own report."""
    out = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    d = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    s = re.search(r"Video: .*?(\d{2,5})x(\d{2,5})", out)
    return {
        "duration": int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0,
        "width": int(s.group(1)) if s else 0,
        "height": int(s.group(2)) if s else 0,
        "audio": "Audio:" in out,
    }


def _font_files() -> dict[int, Path]:
    """Inter at weights 500/700/800, fetched once from Google Fonts and cached."""
    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    wanted = {w: FONT_CACHE / f"Inter-{w}.ttf" for w in (500, 700, 800)}
    if all(p.exists() for p in wanted.values()):
        return wanted
    req = urllib.request.Request(INTER_CSS, headers={"User-Agent": "Mozilla/4.0"})  # old agent: plain TTF links
    css = urllib.request.urlopen(req, timeout=30).read().decode()
    for weight, url in re.findall(r"font-weight: (\d+);.*?url\((https[^)]+\.ttf)\)", css, re.S):
        if int(weight) in wanted:
            urllib.request.urlretrieve(url, wanted[int(weight)])
    missing = [w for w, p in wanted.items() if not p.exists()]
    if missing:
        raise RenderError(f"couldn't fetch Inter weights {missing}")
    return wanted


class Fonts:
    def __init__(self):
        files = _font_files()
        self.kicker = ImageFont.truetype(str(files[700]), 34)
        self.heads = {s: ImageFont.truetype(str(files[800]), s) for s in HEAD_SIZES}
        self.end_head = ImageFont.truetype(str(files[800]), 84)
        self.end_body = ImageFont.truetype(str(files[500]), 44)


def _spaced_width(d: ImageDraw.ImageDraw, text: str, font, gap: int = 5) -> float:
    return sum(d.textlength(c, font=font) + gap for c in text) - gap if text else 0


def layout_headline(fonts: Fonts, text: str) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The largest size that keeps the headline inside the safe area: one line if it fits, else two lines
    (split at the most even word break) that still end above TikTok's caption area."""
    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    fits = lambda line, size: X + d.textlength(line, font=fonts.heads[size]) <= SAFE_RIGHT  # noqa: E731
    for size in HEAD_SIZES:
        if fits(text, size):
            return fonts.heads[size], [text]
    words, seps = text.split(), "·|-–—"
    # Break at a separator first ("UK9.5 · 13 pairs" / "members only"), dropping it; else at the most even space.
    at_sep = [(" ".join(words[:i]), " ".join(words[i + 1:])) for i in range(1, len(words) - 1) if words[i] in seps]
    at_space = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))
                if words[i] not in seps and words[i - 1] not in seps]
    candidates = sorted(at_sep, key=lambda p: abs(len(p[0]) - len(p[1]))) + \
        sorted(at_space, key=lambda p: abs(len(p[0]) - len(p[1])))
    for size in HEAD_SIZES:
        if HEAD_Y + 2 * round(size * LINE_GAP) > TEXT_BOTTOM:
            continue
        for lines in candidates:
            if all(fits(line, size) for line in lines):
                return fonts.heads[size], list(lines)
    raise RenderError(f"headline too long for TikTok's safe area: {text!r}")


def fit_headline(fonts: Fonts, text: str) -> ImageFont.FreeTypeFont:
    return layout_headline(fonts, text)[0]


def check_text_fits(fonts: Fonts, segments: list[Segment]) -> list[str]:
    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    problems = []
    for s in segments:
        if s.headline:
            try:
                fit_headline(fonts, s.headline)
            except RenderError as exc:
                problems.append(str(exc))
        if s.kicker and X + _spaced_width(d, s.kicker, fonts.kicker) > SAFE_RIGHT:
            problems.append(f"label too long for TikTok's safe area: {s.kicker!r}")
    return problems


def _ease(t: float, start: float, length: float = 0.3) -> float:
    p = max(0.0, min(1.0, (t - start) / length))
    return 1 - (1 - p) ** 3


def _faded(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 1:
        return img
    r, g, b, a = img.split()
    img.putalpha(a.point(lambda v: int(v * alpha)))
    return img


class Overlay:
    """Draws the overlay for any moment of the video (RGBA, transparent where the clip shows)."""

    def __init__(self, fonts: Fonts, segments: list[Segment], end_at: float, logo: Path, seal: Path,
                 end_line: str = "£36 a year · link in bio"):
        self.fonts, self.end_at, self.end_line = fonts, end_at, end_line
        self.windows, t = [], 0.0
        for s in segments:
            self.windows.append((t, t + s.duration, s))
            t += s.duration
        lg = Image.open(logo).convert("RGBA")
        self.logo = lg.resize((250, int(lg.height * 250 / lg.width)), Image.LANCZOS)
        self.seal = Image.open(seal).convert("RGBA").resize((520, 520), Image.LANCZOS)
        self.scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(self.scrim)
        for y in range(1000, H):
            sd.line([(0, y), (W, y)], fill=(0, 0, 0, int(215 * min(1, (y - 1000) / 520) ** 1.4)))

    def _text(self, kicker: str, headline: str, dy: int) -> Image.Image:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x = X
        for ch in kicker:
            d.text((x, KICKER_Y + dy), ch, font=self.fonts.kicker, fill=YELLOW)
            x += d.textlength(ch, font=self.fonts.kicker) + 5
        if headline:
            font, lines = layout_headline(self.fonts, headline)
            for n, line in enumerate(lines):
                d.text((X, HEAD_Y + dy + n * round(font.size * LINE_GAP)), line, font=font, fill=WHITE)
        return img

    def _end_card(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (W, H), (*GREEN, 255))
        k = _ease(t, self.end_at, 0.5)
        img.alpha_composite(_faded(self.seal.copy(), k), ((W - 520) // 2, 520 + int(30 * (1 - k))))
        txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(txt)
        kw = _spaced_width(d, "MEMBERS ONLY", self.fonts.kicker)
        x = (W - kw) / 2
        for ch in "MEMBERS ONLY":
            d.text((x, 1110), ch, font=self.fonts.kicker, fill=YELLOW)
            x += d.textlength(ch, font=self.fonts.kicker) + 5
        head = "Join the Clubhouse."
        d.text(((W - d.textlength(head, font=self.fonts.end_head)) / 2, 1160), head, font=self.fonts.end_head, fill=WHITE)
        d.text(((W - d.textlength(self.end_line, font=self.fonts.end_body)) / 2, 1275), self.end_line,
               font=self.fonts.end_body, fill=(225, 238, 230))
        img.alpha_composite(_faded(txt, _ease(t, self.end_at + 0.35, 0.45)))
        return img

    def frame(self, t: float) -> Image.Image:
        if t >= self.end_at:
            return self._end_card(t)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        img.alpha_composite(self.scrim)
        img.alpha_composite(self.logo, (X, 200))
        for start, end, s in self.windows:
            if start <= t < end and (s.kicker or s.headline):
                k = _ease(t, start)
                img.alpha_composite(_faded(self._text(s.kicker, s.headline, int(28 * (1 - k))), k))
        return img


def render(segments: list[Segment], voice: Path, out: Path, work: Path, end_seconds: float = END_CARD_SECONDS) -> Path:
    """Cut the clips to the voice, lay the overlays over them and add the end card.

    ``end_seconds`` is how long the end card stays up: long enough for any of the
    voice still playing (the call to action is spoken over it)."""
    fonts = Fonts()
    problems = check_text_fits(fonts, segments)
    if problems:
        raise RenderError("; ".join(problems))
    body = sum(s.duration for s in segments)
    end_seconds = max(END_CARD_SECONDS, end_seconds)
    total = body + end_seconds
    clips = sorted({s.clip for s in segments})
    index = {c: i for i, c in enumerate(clips)}
    norm = f"fps={FPS},scale=-2:{H},crop={W}:{H},setsar=1"
    parts = []
    for k, s in enumerate(segments):
        parts.append(f"[{index[s.clip]}:v]trim={s.start:.3f}:{s.start + s.duration * s.speed:.3f},"
                     f"setpts=(PTS-STARTPTS)/{s.speed:.4f},{norm},trim=duration={s.duration:.3f}[s{k}]")
    parts.append(f"color=c=0x016747:s={W}x{H}:r={FPS}:d={end_seconds + 0.1},setsar=1[g]")
    parts.append("".join(f"[s{k}]" for k in range(len(segments))) + f"[g]concat=n={len(segments) + 1}:v=1:a=0[v]")
    base = work / "base.mp4"
    cmd = [ffmpeg(), "-v", "error", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-t", f"{total:.3f}", "-c:v", "libx264", "-crf", "16",
            "-pix_fmt", "yuv420p", str(base)]
    subprocess.run(cmd, check=True)
    overlay = Overlay(fonts, segments, body, ASSETS / "brand" / "logo.png", ASSETS / "brand" / "clubhouse-seal-white.png")
    proc = subprocess.Popen(
        [ffmpeg(), "-v", "error", "-y", "-i", str(base), "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-i", str(voice), "-filter_complex",
         "[0:v][1:v]overlay=0:0:format=auto[v];[2:a]apad=pad_dur=2,loudnorm=I=-14:TP=-1.5[a]",
         "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
        stdin=subprocess.PIPE,
    )
    for i in range(int(total * FPS)):
        proc.stdin.write(overlay.frame(i / FPS).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RenderError("ffmpeg failed while adding overlays")
    return out


def frames(path: Path, times: list[float], work: Path, width: int = 512) -> list[Path]:
    """Stills from a clip, for the visual check."""
    out = []
    for i, t in enumerate(times):
        p = work / f"{path.stem}_f{i}.jpg"
        subprocess.run([ffmpeg(), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={width}:-1", str(p)], check=True)
        out.append(p)
    return out


PHOTO_BG = (255, 255, 255)   # the white of Shopify product photos, so boxed photos and cut-outs both sit cleanly


def photo_still(photos: list[Path], out: Path) -> Path:
    """The product's own photos laid out on a light 9:16 card, above the text area: one large, two side by side,
    three or four in a grid. Free to make, and it can't get a product wrong."""
    canvas = Image.new("RGB", (W, H), PHOTO_BG)
    imgs = [Image.open(p).convert("RGBA") for p in photos[:4]]   # PNG cut-outs keep their transparency
    top, bottom = 380, 1250           # below the logo, above the headline
    cols = 1 if len(imgs) == 1 else 2
    rows = (len(imgs) + cols - 1) // cols
    cell_w, cell_h = (W - 2 * 50) // cols, (bottom - top) // rows
    for i, img in enumerate(imgs):
        img.thumbnail((cell_w - 20, cell_h - 20), Image.LANCZOS)
        r, c = divmod(i, cols)
        x = 50 + c * cell_w + (cell_w - img.width) // 2
        y = top + r * cell_h + (cell_h - img.height) // 2
        canvas.paste(img, (x, y), img)
    # A soft dark band at the top so the white logo reads on a white card.
    band = Image.new("RGBA", (W, 480), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    for y in range(480):
        bd.line([(0, y), (W, y)], fill=(17, 17, 17, int(235 * min(1, (1 - y / 480) * 1.6) ** 1.3)))
    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(band)
    canvas.convert("RGB").save(out, quality=92)
    return out


def photo_clip(photos: list[Path], seconds: float, out: Path, work: Path, push_in: bool = True) -> Path:
    """A slow push-in (or pull-back) on the product photos: the free alternative to an AI clip."""
    still = photo_still(photos, work / f"{out.stem}_still.jpg")
    frames_n = max(1, round(seconds * FPS))
    zoom = (f"1+0.06*on/{frames_n}" if push_in else f"1.06-0.06*on/{frames_n}")
    # Upscaling first keeps zoompan's whole-pixel steps from shimmering.
    vf = (f"scale={W * 2}:{H * 2},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames_n}:s={W}x{H}:fps={FPS},format=yuv420p")
    subprocess.run([ffmpeg(), "-v", "error", "-y", "-loop", "1", "-i", str(still), "-vf", vf,
                    "-frames:v", str(frames_n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)],
                   check=True)
    return out

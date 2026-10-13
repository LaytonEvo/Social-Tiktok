"""Video job: ``python -m evo_tiktok.video [--dry-run] [--week-start YYYY-MM-DD] [--script N]``.

Turns each week's approved scripts into draft AI videos for Layton to review,
following ``docs/VIDEO_STYLE.md``:

1. Plan: Claude turns the script's shots into AI shots (products only, in the
   Evo studio); the voice and on-screen headlines come from the script itself.
2. Check the plan against today's stock and member prices (no £, savings true
   for the products shown, in stock and buyable, no brand next to a discount,
   no headline repeating the voice, products only).
3. Make it: Charlotte's voiceover (transcribed back and compared with the
   script), one AI clip per shot from the product's own photos (each checked
   against those photos by Claude), style D overlays and the Clubhouse end card.
4. Store the MP4, record the version and set the Production row to Ready to
   review with its Preview link and anything Layton should look at.

When Layton sets Needs changes with notes in Edit request, the next run turns
the notes into a revised plan (only changes the rules allow), checks it again
and makes the next version. Nothing here posts to TikTok, and the job does
nothing unless switched on (``VIDEO_ENABLED=1`` on the service or ``video.enabled`` in settings).
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import logging
import math
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import production, stock, validators
from .config import ROOT, Settings
from .fal import Fal
from .llm import ClaudeJSON, JSONModel, load_prompt
from .models import StockLine
from .runner import JobContext, run_job
from .storage import store as make_store
from .video_render import Segment, frames, probe, render

log = logging.getLogger(__name__)

PEOPLE_RE = re.compile(
    r"\b(?:people|person|man|men|woman|women|golfers?|players?|hands?|fingers?|faces?|child(?:ren)?|kids?|"
    r"caddie|someone|customer|staff|presenter)\b",
    re.IGNORECASE,
)
SETTINGS_ALLOWED = ("studio", "outdoors")
STUDIO_REFS = [ROOT / "assets" / "video" / "studio-mat.jpg", ROOT / "assets" / "video" / "studio-lights.jpg"]

SHOT_FIELDS = {
    "kicker": {"type": "string"},
    "prompt": {"type": "string"},
    "skus": {"type": "array", "items": {"type": "string"}},
    "setting": {"type": "string", "enum": list(SETTINGS_ALLOWED)},
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {"shots": {"type": "array", "items": {
        "type": "object", "properties": SHOT_FIELDS, "required": list(SHOT_FIELDS), "additionalProperties": False}}},
    "required": ["shots"],
    "additionalProperties": False,
}
EDIT_SHOT_FIELDS = SHOT_FIELDS | {"say": {"type": "string"}, "headline": {"type": "string"}}
EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {"type": "array", "items": {"type": "object", "properties": EDIT_SHOT_FIELDS,
                                             "required": list(EDIT_SHOT_FIELDS), "additionalProperties": False}},
        "summary": {"type": "string"},
        "refused": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["shots", "summary", "refused"],
    "additionalProperties": False,
}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}}},
    "required": ["ok", "issues"],
    "additionalProperties": False,
}


def cfg(settings: Settings) -> dict:
    return settings.get("video") or {}


def enabled(settings: Settings) -> bool:
    """On when settings say so, or when the service sets VIDEO_ENABLED (1/true/yes); VIDEO_ENABLED=0 forces it off."""
    flag = (settings.secret("VIDEO_ENABLED", required=False) or "").strip().lower()
    if flag:
        return flag in ("1", "true", "yes", "on")
    return bool(cfg(settings).get("enabled"))


# -------------------------------------------------------------------- plans


@dataclass
class Shot:
    say: str
    headline: str
    kicker: str
    prompt: str
    skus: list[str]
    setting: str = "studio"
    seconds: float = 4.0     # the script's timing, used where nothing is said


@dataclass
class VideoPlan:
    week_start: str
    script_no: int
    format: str
    caption: str
    shots: list[Shot] = field(default_factory=list)

    @property
    def voice_text(self) -> str:
        return " ".join(s.say.strip() for s in self.shots if s.say.strip())

    @property
    def skus(self) -> list[str]:
        return sorted({k for s in self.shots for k in s.skus})

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "VideoPlan":
        d = json.loads(text)
        return cls(d["week_start"], d["script_no"], d["format"], d["caption"], [Shot(**s) for s in d["shots"]])


def source_hash(row: dict) -> str:
    """Identifies the script a video was made from; a regenerated script is a new video."""
    raw = json.dumps([row.get("hook"), row.get("shots"), row.get("featured_skus")], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _product(line: StockLine) -> dict:
    return {"sku": line.sku, "product": line.product_title, "category": line.category, "size": line.size or ""}


def plan_from_script(llm: JSONModel, row: dict, stock_by_sku: dict[str, StockLine]) -> VideoPlan:
    shots = json.loads(row["shots"] or "[]")
    skus = json.loads(row["featured_skus"] or "[]")
    products = [_product(stock_by_sku[k]) for k in skus if k in stock_by_sku]
    prompt = load_prompt("video_shots", {
        "FORMAT": row["format"],
        "SHOTS_JSON": json.dumps([{"say": s["say"], "on_screen": s["on_screen"], "film": s["film"]} for s in shots],
                                 ensure_ascii=False),
        "PRODUCTS_JSON": json.dumps(products, ensure_ascii=False),
    })
    data, _ = llm.complete_json(prompt.system, [{"role": "user", "content": prompt.user}], PLAN_SCHEMA, max_tokens=8000)
    planned = data["shots"]
    if len(planned) != len(shots):
        raise validators.GuardrailError([f"shot plan has {len(planned)} shots for a {len(shots)}-shot script"])
    return VideoPlan(
        str(row["week_start"]), int(row["script_no"]), row["format"], row["caption"],
        [Shot(say=s["say"], headline=s["on_screen"], kicker=p["kicker"].strip().upper(), prompt=p["prompt"],
              skus=p["skus"], setting=p["setting"], seconds=float(s.get("seconds") or 4))
         for s, p in zip(shots, planned)],
    )


def edit_plan(llm: JSONModel, plan: VideoPlan, request: str, candidates: list[StockLine]) -> tuple[VideoPlan, str, list[str]]:
    """Layton's notes as a revised plan. Returns (plan, summary, refused)."""
    products = []
    for line in candidates:
        products.append(_product(line) | {"members_save_pct": int(line.member_saving_pct or 0),
                                          "facts": (line.description or "")[:300]})
    prompt = load_prompt("video_edit", {
        "EDIT_REQUEST": request.strip(),
        "FORMAT": plan.format,
        "PLAN_JSON": json.dumps([asdict(s) for s in plan.shots], ensure_ascii=False),
        "PRODUCTS_JSON": json.dumps(products, ensure_ascii=False),
    })
    data, _ = llm.complete_json(prompt.system, [{"role": "user", "content": prompt.user}], EDIT_SCHEMA, max_tokens=8000)
    old = {i: s for i, s in enumerate(plan.shots)}
    shots = [Shot(say=s["say"], headline=s["headline"], kicker=s["kicker"].strip().upper(), prompt=s["prompt"],
                  skus=s["skus"], setting=s["setting"], seconds=old.get(i, Shot("", "", "", "", [])).seconds)
             for i, s in enumerate(data["shots"])]
    return VideoPlan(plan.week_start, plan.script_no, plan.format, plan.caption, shots), data["summary"], data["refused"]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", (text or "").lower()).strip()


def plan_errors(plan: VideoPlan, stock_by_sku: dict[str, StockLine], settings: Settings,
                allowed: set[str] | None = None) -> list[str]:
    """Every rule the plan breaks against today's stock and member prices; empty means it can be made."""
    e: list[str] = []
    if not 3 <= len(plan.shots) <= 6:
        e.append(f"a video needs 3 to 6 shots, not {len(plan.shots)}")
    voice, heads = plan.voice_text, [s.headline for s in plan.shots if s.headline.strip()]
    lines: list[StockLine] = []
    for n, s in enumerate(plan.shots, 1):
        if not s.skus:
            e.append(f"shot {n} shows no product (AI footage must show a real product)")
        for k in s.skus:
            line = stock_by_sku.get(k)
            if allowed is not None and k not in allowed:
                e.append(f"shot {n}: {k} isn't one of the products this video may show")
            elif line is None or not line.buyable_online:
                e.append(f"shot {n}: {k} isn't in stock and buyable online today")
            else:
                lines.append(line)
        if PEOPLE_RE.search(s.prompt):
            e.append(f"shot {n} asks for a person ('{PEOPLE_RE.search(s.prompt).group()}'); products only")
        if s.setting not in SETTINGS_ALLOWED:
            e.append(f"shot {n} setting must be studio or outdoors")
        if len(s.kicker) > 24 or "£" in s.kicker or "%" in s.kicker:
            e.append(f"shot {n} label must be a few words with no £ or %")
        for text in [s.kicker, s.headline]:
            e += validators.brand_price_check(text, settings.restricted_brands, settings.brand_price_window)
        if s.headline and len(_norm(s.headline).split()) >= 2 and _norm(s.headline) in _norm(voice):
            e.append(f"shot {n} headline repeats the voice ('{s.headline}')")
    everything = "\n".join([voice, *heads, plan.caption])
    e += validators.no_pound_check(everything)
    e += validators.member_discount_check(everything, lines)
    if plan.shots and "clubhouse" not in plan.shots[-1].say.lower():
        e.append("the last shot must end with the call to action: join the Clubhouse, link in bio")
    return list(dict.fromkeys(e))


# --------------------------------------------------------------- making it


def product_photos(line: StockLine, http: httpx.Client, count: int = 2) -> list[str]:
    """The product's own photos from its page on evolutiongolf.co.uk."""
    if not line.url:
        return []
    resp = http.get(line.url.rstrip("/") + ".js", follow_redirects=True)
    if resp.status_code >= 400:
        return []
    urls = []
    for src in resp.json().get("images", []):
        url = ("https:" + src) if src.startswith("//") else src
        urls.append(url.split("?")[0])
    return urls[:count]


def _data_uri(path: Path, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def words_from_timestamps(segments: list[dict]) -> list[tuple[str, float, float]]:
    words, cur = [], None
    for seg in segments or []:
        for c, s, e in zip(seg["characters"], seg["character_start_times_seconds"], seg["character_end_times_seconds"]):
            if c.isspace():
                if cur:
                    words.append(tuple(cur))
                    cur = None
                continue
            if cur is None:
                cur = [c, s, e]
            else:
                cur[0] += c
                cur[2] = e
    if cur:
        words.append(tuple(cur))
    return words


def shot_windows(plan: VideoPlan, words: list[tuple[str, float, float]], end: float) -> list[tuple[float, float]]:
    """When each shot is on screen: it starts as its own line starts. Silent shots share time by the script's seconds."""
    counts = [len(s.say.split()) for s in plan.shots]
    anchors: list[float | None] = []
    if words and len(words) == sum(counts):
        i = 0
        for n in counts:
            anchors.append(words[i][1] if n else None)
            i += n
    else:  # timings don't line up word for word: share the speech by how much each shot says
        chars = [len(s.say.strip()) for s in plan.shots]
        total, done = sum(chars) or 1, 0
        for n in chars:
            anchors.append(end * done / total if n else None)
            done += n
    anchors[0] = 0.0
    starts = list(anchors)
    k = 0
    while k < len(starts):
        if starts[k] is None:
            j = k
            while j < len(starts) and starts[j] is None:
                j += 1
            lo = starts[k - 1]
            hi = starts[j] if j < len(starts) else end
            run = plan.shots[k - 1:j]
            total = sum(s.seconds for s in run) or 1
            t = lo
            for m in range(k - 1, j - 1):
                t += (hi - lo) * plan.shots[m].seconds / total
                starts[m + 1] = t
            k = j
        k += 1
    bounds = starts + [end]
    return [(bounds[n], bounds[n + 1]) for n in range(len(plan.shots))]


@dataclass
class Result:
    ok: bool
    file: Path | None = None
    checks: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cost: float = 0.0


class Maker:
    """Makes one version of a video. Collaborators are injectable for tests."""

    def __init__(self, settings: Settings, llm: JSONModel, fal: Fal, store, http: httpx.Client, work: Path):
        self.settings, self.llm, self.fal, self.store, self.http, self.work = settings, llm, fal, store, http, work
        c = cfg(settings)
        self.video_model = c.get("model", "minimax/h3-max/reference-to-video")
        self.resolution = c.get("resolution", "768P")
        self.tts_model = c.get("tts", "elevenlabs/tts/eleven-v4")
        self.voice_name = c.get("voice", "Charlotte")
        self.transcriber = c.get("transcribe", "fal-ai/whisper")
        self.clip_usd_per_second = float(c.get("clip_usd_per_second", 0.05))
        self.min_speed = float(c.get("min_speed", 0.7))
        self.cost = 0.0
        self._photos: dict[str, list[str]] = {}

    # voice ---------------------------------------------------------------
    def voice(self, text: str) -> tuple[Path, list[tuple[str, float, float]]]:
        res = self.fal.run(self.tts_model, {"text": text, "voice": self.voice_name, "language_code": "en",
                                            "stability": 0.4, "timestamps": True})
        self.cost += 0.08 * len(text) / 1000
        path = self.fal.download(res["audio"]["url"], self.work / "voice.mp3")
        return path, words_from_timestamps(res.get("timestamps") or [])

    def transcript_match(self, audio: Path, text: str) -> float:
        res = self.fal.run(self.transcriber, {"audio_url": _data_uri(audio, "audio/mpeg"), "task": "transcribe",
                                              "language": "en"})
        self.cost += 0.01
        heard = _norm(res.get("text", "")).split()
        return difflib.SequenceMatcher(None, _norm(text).split(), heard).ratio()

    # clips ---------------------------------------------------------------
    def photos(self, line: StockLine, count: int) -> list[str]:
        if line.sku not in self._photos:
            self._photos[line.sku] = product_photos(line, self.http, count=2)
        return self._photos[line.sku][:count]

    def refs(self, shot: Shot, stock_by_sku: dict[str, StockLine]) -> list[str]:
        refs = []
        for k in shot.skus:
            refs += self.photos(stock_by_sku[k], 2 if len(shot.skus) == 1 else 1)
        if shot.setting == "studio":
            refs += [_data_uri(p) for p in STUDIO_REFS]
        return refs[:8]

    def clip_body(self, shot: Shot, refs: list[str], seconds: int) -> dict:
        studio = ("The indoor golf studio from the last two reference images: bright green artificial turf, dark grey "
                  "walls, golf simulator screen and hexagonal LED ceiling lights softly out of focus. "
                  if shot.setting == "studio" else "")
        return {
            "prompt": f"{shot.prompt} {studio}Shot on a phone, vertical, crisp product detail. Every product must look "
                      "exactly like its reference photos: same shape, colours, sole, logo placement. No text, no people, "
                      "no hands, no extra products.",
            "reference_image_urls": refs, "aspect_ratio": "9:16", "duration": seconds, "resolution": self.resolution,
        }

    def clips(self, shots: list[tuple[int, Shot, float]], stock_by_sku: dict[str, StockLine],
              fresh: set[int] = frozenset()) -> dict[int, Path]:
        """One clip per shot, reused from the store when an identical shot was made before."""
        out, todo = {}, {}
        for n, shot, duration in shots:
            seconds = int(min(10, max(3, math.ceil(duration + 0.3))))
            refs = self.refs(shot, stock_by_sku)
            body = self.clip_body(shot, refs, seconds)
            key = "clips/" + hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
            key += f"-{n}r.mp4" if n in fresh else ".mp4"
            path = self.work / key.replace("/", "_")
            if n not in fresh and self.store.get(key, path):
                out[n] = path
                continue
            if n in fresh:
                body["seed"] = int(hashlib.sha1(key.encode()).hexdigest()[:6], 16)
            todo[n] = (key, path, body)
        results = self.fal.run_many({str(n): (self.video_model, body) for n, (_, _, body) in todo.items()})
        for n, (key, path, body) in todo.items():
            self.fal.download(results[str(n)]["video"]["url"], path)
            self.store.put(path, key)
            self.cost += self.clip_usd_per_second * body["duration"]
            out[n] = path
        return out

    def visual_check(self, clip: Path, shot: Shot, stock_by_sku: dict[str, StockLine]) -> dict:
        length = probe(clip)["duration"] or 5
        stills = frames(clip, [length * 0.15, length * 0.5, length * 0.85], self.work)
        refs = []
        for k in shot.skus:
            refs += self.photos(stock_by_sku[k], 1)
        prompt = load_prompt("video_check", {
            "N_REFS": len(refs), "PRODUCTS": ", ".join(stock_by_sku[k].product_title for k in shot.skus),
            "SHOT": shot.prompt})
        content = [{"type": "image", "source": {"type": "url", "url": u}} for u in refs]
        content += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(p.read_bytes()).decode()}} for p in stills]
        content.append({"type": "text", "text": prompt.user})
        data, _ = self.llm.complete_json(prompt.system, [{"role": "user", "content": content}], CHECK_SCHEMA,
                                         max_tokens=4000)
        self.cost += 0.03
        return data

    # the whole video -----------------------------------------------------
    def make(self, plan: VideoPlan, stock_by_sku: dict[str, StockLine], out: Path) -> Result:
        res = Result(ok=False)
        voice, words = self.voice(plan.voice_text)
        match = self.transcript_match(voice, plan.voice_text)
        if match < 0.85:
            voice, words = self.voice(plan.voice_text)   # one retake
            match = self.transcript_match(voice, plan.voice_text)
        res.checks["voice_matches_script"] = round(match, 3)
        if match < 0.85:
            res.flags.append(f"the voice doesn't quite match the script ({match:.0%}); listen closely")
        speech_end = (words[-1][2] if words else probe(voice)["duration"]) + 0.4
        windows = shot_windows(plan, words, speech_end)
        # The call to action plays over the end card, so the last shot needs no footage.
        last = plan.shots[-1]
        cta_on_card = "clubhouse" in last.say.lower()
        body_shots = list(enumerate(plan.shots))[:-1] if cta_on_card else list(enumerate(plan.shots))
        spans = [(n, s, windows[n][1] - windows[n][0]) for n, s in body_shots]
        for n, s, d in spans:
            if d > 10 / self.min_speed:
                res.errors.append(f"shot {n + 1} runs {d:.1f}s; split it (AI clips are at most 10s)")
        if res.errors:
            return res
        clips = self.clips(spans, stock_by_sku)
        checks = {}
        for n, s, d in spans:
            verdict = self.visual_check(clips[n], s, stock_by_sku)
            if not verdict["ok"]:   # one more try with a new seed
                clips.update(self.clips([(n, s, d)], stock_by_sku, fresh={n}))
                verdict = self.visual_check(clips[n], s, stock_by_sku)
                if not verdict["ok"]:
                    res.flags.append(f"shot {n + 1}: " + "; ".join(verdict["issues"]))
            checks[f"shot_{n + 1}"] = verdict
        res.checks["visual"] = checks
        segments = []
        for n, s, d in spans:
            length = probe(clips[n])["duration"] or 5
            speed = min(1.0, (length - 0.05) / d)
            segments.append(Segment(clips[n], 0.0, d, speed, s.kicker if s.headline else "", s.headline))
        body_end = sum(seg.duration for seg in segments)
        voice_end = probe(voice)["duration"]
        render(segments, voice, out, self.work, end_seconds=max(voice_end, speech_end) - body_end + 0.8)
        info = probe(out)
        res.checks["format"] = info
        tech = []
        if (info["width"], info["height"]) != (1080, 1920):
            tech.append(f"frame is {info['width']}x{info['height']}, not 1080x1920")
        if not 15 <= info["duration"] <= 40:
            tech.append(f"video runs {info['duration']:.1f}s; keep it 15 to 40s")
        if not info["audio"]:
            tech.append("no sound track")
        res.errors += tech
        res.ok, res.file, res.cost = not res.errors, out, round(self.cost, 2)
        return res


# --------------------------------------------------------------------- job


def script_rows(db, since: date) -> list[dict]:
    cols = ["week_start", "script_no", "format", "hook", "caption", "featured_skus", "shots", "status",
            "edit_request", "approved_version"]
    rows = db.query(f"SELECT {', '.join(cols)} FROM content_log WHERE week_start >= ? ORDER BY week_start, script_no",
                    (since.isoformat(),))
    return [dict(zip(cols, r)) for r in rows]


def latest_version(db, week: str, script_no: int) -> dict | None:
    cols = ["version", "source", "status", "edit_request", "plan", "cost_usd"]
    rows = db.query(f"SELECT {', '.join(cols)} FROM video_version WHERE week_start = ? AND script_no = ? "
                    "ORDER BY version DESC LIMIT 1", (week, script_no))
    return dict(zip(cols, rows[0])) if rows else None


def spent_this_week(db) -> float:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = db.query("SELECT COALESCE(SUM(cost_usd), 0) FROM video_version WHERE created_at >= ?", (since,))
    return float(rows[0][0] or 0)


def work_to_do(row: dict, latest: dict | None, max_versions: int) -> tuple[str, str] | None:
    """('make', '') for a first draft, ('edit', notes) for a revision, or None."""
    src = source_hash(row)
    if row["status"] == "planned" and (latest is None or latest["source"] != src):
        return ("make", "")
    if row["status"] == "changes" and latest and latest["source"] == src:
        notes = (row.get("edit_request") or "").strip()
        if notes and notes != (latest.get("edit_request") or "").strip():
            if latest["version"] >= max_versions:
                return ("limit", notes)
            return ("edit", notes)
    return None


def record(db, plan: VideoPlan, version: int, source: str, status: str, edit_request: str, key: str | None,
           url: str | None, checks: dict, flags: list[str], cost: float) -> None:
    db.execute(
        "INSERT INTO video_version (week_start, script_no, version, source, status, edit_request, plan, file_key, "
        "preview_url, checks, flags, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plan.week_start, plan.script_no, version, source, status, edit_request or None, plan.to_json(), key, url,
         json.dumps(checks, default=str), json.dumps(flags), cost, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def candidates_for_swap(lines: list[StockLine]) -> list[StockLine]:
    seen, out = set(), []
    for line in sorted(lines, key=lambda x: -(x.member_saving_pct or 0)):
        if line.in_portal and line.buyable_online and line.product_title not in seen:
            seen.add(line.product_title)
            out.append(line)
    return out[:40]


def body(ctx: JobContext) -> None:
    settings = ctx.settings
    c = cfg(settings)
    if not enabled(settings):
        log.info("Video job is off (video.enabled is false and VIDEO_ENABLED isn't set): nothing made")
        ctx.summary["disabled"] = True
        return
    read = ctx.read_db()
    if read is None:
        raise ValueError("DATABASE_URL is needed to make videos")
    if not ctx.dry_run:
        try:
            production.sync(settings, read)
        except Exception as exc:  # stale statuses only delay a draft by an hour
            log.warning("Production tab not synced before making videos: %s", exc)
    lines = stock.load_latest_snapshot(read)
    if not lines:
        raise ValueError("No stock snapshot from the last 30 hours: run the stock job first")
    stock_by_sku = {line.sku: line for line in lines}
    tz = ZoneInfo(settings["timezone"])
    today = datetime.now(tz).date()
    since = date.fromisoformat(ctx.args.week_start) if ctx.args.week_start else today - timedelta(days=today.weekday() + 7)
    rows = [r for r in script_rows(read, since) if not ctx.args.script or int(r["script_no"]) == ctx.args.script]
    max_versions = int(c.get("max_versions", 3))
    budget = float(c.get("weekly_budget_usd", 20))
    est = float(c.get("estimate_usd_per_video", 1.5))
    llm = ctx.llm or ClaudeJSON(settings, "video")
    made = []
    for row in rows:
        week, no = str(row["week_start"]), int(row["script_no"])
        latest = latest_version(read, week, no)
        todo = work_to_do(row, latest, max_versions)
        if todo is None:
            continue
        kind, notes = todo
        if kind == "limit":
            log.warning("Week %s video %s: edit limit (%d versions) reached", week, no, max_versions)
            if not ctx.dry_run:
                production.set_cells(settings, week, no, {"Notes": f"Edit limit of {max_versions} versions reached: "
                                                                   "message Claude to carry on"})
            continue
        if not ctx.dry_run and spent_this_week(read) + est > budget:
            log.error("Weekly video budget ($%.0f) reached: week %s video %s not made", budget, week, no)
            production.set_cells(settings, week, no, {"Notes": f"Weekly video budget (${budget:.0f}) reached"})
            break
        version = (latest["version"] + 1) if latest else 1
        refused, summary = [], ""
        if kind == "make":
            plan = plan_from_script(llm, row, stock_by_sku)
            allowed = set(json.loads(row["featured_skus"] or "[]"))
        else:
            current = VideoPlan.from_json(latest["plan"])
            plan, summary, refused = edit_plan(llm, current, notes, candidates_for_swap(lines))
            allowed = {line.sku for line in candidates_for_swap(lines)} | set(current.skus)
        errors = plan_errors(plan, stock_by_sku, settings, allowed)
        log.info("Week %s video %s v%d (%s): %d shots, voice %r%s", week, no, version, kind, len(plan.shots),
                 plan.voice_text[:80], f"; problems: {errors}" if errors else "")
        if ctx.dry_run:
            made.append({"week": week, "video": no, "version": version, "plan": json.loads(plan.to_json()),
                         "errors": errors, "refused": refused})
            continue
        db = ctx.db()
        if errors:
            record(db, plan, version, source_hash(row), "failed", notes, None, None, {"plan": errors}, errors, 0)
            production.set_cells(settings, week, no, {"Notes": "Can't make this video: " + "; ".join(errors)})
            continue
        with tempfile.TemporaryDirectory() as tmp:
            maker = Maker(settings, llm, Fal(settings), make_store(settings), httpx.Client(timeout=60), Path(tmp))
            out = Path(tmp) / f"{week}-{no}-v{version}.mp4"
            result = maker.make(plan, stock_by_sku, out)
            flags = result.flags + [f"not changed: {r}" for r in refused]
            if not result.ok:
                record(db, plan, version, source_hash(row), "failed", notes, None, None, result.checks,
                       result.errors, result.cost)
                production.set_cells(settings, week, no, {"Notes": "Video failed its checks: " + "; ".join(result.errors)})
                continue
            key = f"videos/{week}/{no}/v{version}.mp4"
            st = make_store(settings)
            st.put(result.file, key)
            url = st.url(key)
        record(db, plan, version, source_hash(row), "review", notes, key, url, result.checks, flags, result.cost)
        db.execute("UPDATE content_log SET status = 'review', approved_version = NULL WHERE week_start = ? "
                   "AND script_no = ?", (week, no))
        db.commit()
        note = (f"v{version}: {summary}. " if summary else f"v{version}. ") + (
            "Check: " + "; ".join(flags) if flags else "All checks passed.")
        production.set_cells(settings, week, no, {"Status": "Ready to review", "Version": f"v{version}",
                                                  "Preview": url, "Notes": note})
        made.append({"week": week, "video": no, "version": version, "cost": result.cost, "flags": flags})
    ctx.summary.update(videos=made)


def add_args(parser) -> None:
    parser.add_argument("--week-start", help="only scripts from this Monday (YYYY-MM-DD) onwards")
    parser.add_argument("--script", type=int, help="only this video number")


def main(argv=None, llm: JSONModel | None = None, settings: Settings | None = None) -> int:
    def _body(ctx: JobContext) -> None:
        ctx.llm = llm
        body(ctx)

    return run_job("video", _body, argv, add_args, settings)


if __name__ == "__main__":
    raise SystemExit(main())

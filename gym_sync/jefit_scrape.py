"""Modern Jefit public-log scraper (Python port of denolfe/jefit ideas).

Requires the member profile privacy to be set to Everyone.
Official Jefit has no public API — this scrapes the web calendar/log pages.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BASE = "https://www.jefit.com"


class JefitScrapeError(RuntimeError):
    """Raised when Jefit pages cannot be scraped (private, changed HTML, network)."""


@dataclass
class ExerciseSet:
    index: int
    weight: float | None = None
    reps: int | None = None
    duration_sec: int | None = None


@dataclass
class ScrapedExercise:
    name: str
    exercise_type: str = "Lift"
    one_rep_max: float | None = None
    sets: list[ExerciseSet] = field(default_factory=list)

    def logs_string(self) -> str:
        parts: list[str] = []
        for s in self.sets:
            if s.weight is not None and s.reps is not None:
                parts.append(f"{s.weight:g}x{s.reps}")
            elif s.reps is not None:
                parts.append(f"0x{s.reps}")
            elif s.duration_sec is not None:
                parts.append(f"0x{s.duration_sec}")
        return ",".join(parts)


@dataclass
class ScrapedWorkout:
    date: str
    session_length_sec: int | None = None
    actual_workout_sec: int | None = None
    exercises_done: int | None = None
    weight_lifted: float | None = None
    exercises: list[ScrapedExercise] = field(default_factory=list)
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "session_length_sec": self.session_length_sec,
            "actual_workout_sec": self.actual_workout_sec,
            "exercises_done": self.exercises_done,
            "weight_lifted": self.weight_lifted,
            "source_url": self.source_url,
            "exercises": [
                {
                    "name": e.name,
                    "type": e.exercise_type,
                    "one_rep_max": e.one_rep_max,
                    "sets": [
                        {
                            "index": s.index,
                            "weight": s.weight,
                            "reps": s.reps,
                            "duration_sec": s.duration_sec,
                        }
                        for s in e.sets
                    ],
                    "logs": e.logs_string(),
                }
                for e in self.exercises
            ],
        }


def _fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise JefitScrapeError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise JefitScrapeError(f"Network error fetching {url}: {exc.reason}") from exc


def calendar_url(user_id: str | int, year: int, month: int) -> str:
    return f"{BASE}/members/user-logs/?xid={user_id}&yy={year}&mm={month}"


def log_url(user_id: str | int, day: date | str) -> str:
    if isinstance(day, date):
        dd = day.isoformat()
    else:
        dd = str(day)
    return f"{BASE}/members/user-logs/log/?xid={user_id}&dd={dd}"


def check_public(user_id: str | int) -> tuple[bool, str]:
    """Return (logs_are_public, detail_message).

    Note: Jefit profile privacy and training-log privacy can differ.
    Scraping needs the **Logs** calendar to be viewable anonymously.
    """
    today = date.today()
    html = _fetch(calendar_url(user_id, today.year, today.month))
    low = html.lower()
    if "keep it private" in low or "don't have permission" in low or "do not have permission" in low:
        return (
            False,
            "Training logs are still private (profile public is not enough). "
            "In the Jefit app go to Profile -> Settings -> Privacy and set "
            "who can view your training logs / workout logs to Everyone "
            "(not only profile/community). Then re-run: python sync.py jefit-status",
        )
    # Positive signal: calendar day markers or log links present
    if (
        "calenderday" in low
        or "calendarday" in low
        or "fixedlogbar" in low
        or re.search(r"dd=\d{4}-\d{2}-\d{2}", html)
        or re.search(r"\d{4}-\d{2}-\d{2}", html)
    ):
        return True, "Training logs calendar is publicly readable."
    return (
        False,
        "Logs page loaded but no workout calendar markers found. "
        "Confirm privacy for training logs is Everyone, then try again.",
    )


_DATE_IN_HREF = re.compile(r"(?:dd=|/)(\d{4}-\d{2}-\d{2})")
_IMG_DAY = re.compile(
    r'<a[^>]+href="([^"]+)"[^>]*>\s*<img[^>]*>',
    re.IGNORECASE,
)


def list_logged_dates(user_id: str | int, year: int, month: int) -> list[str]:
    """Dates with logged workouts in a month calendar (YYYY-MM-DD)."""
    html = _fetch(calendar_url(user_id, year, month))
    if "keep it private" in html.lower() or "don't have permission" in html.lower():
        raise JefitScrapeError(
            "Jefit profile is private — cannot list workout days. "
            "Set privacy to Everyone, or use: python sync.py jefit-sync --csv-only"
        )

    found: list[str] = []
    for match in _IMG_DAY.finditer(html):
        href = match.group(1)
        dm = _DATE_IN_HREF.search(href)
        if dm:
            found.append(dm.group(1))
    # Fallback: any dd= dates on page
    if not found:
        found = _DATE_IN_HREF.findall(html)
    # Unique, sorted
    return sorted(set(found))


def _parse_time_to_seconds(text: str) -> int | None:
    text = text.strip()
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", text)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _parse_set_line(line: str) -> ExerciseSet | None:
    line = line.strip()
    m = re.match(r"(\d+)\s*:?\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+)", line)
    if m:
        return ExerciseSet(
            index=int(m.group(1)),
            weight=float(m.group(2)),
            reps=int(m.group(3)),
        )
    m = re.match(r"(\d+)\s*:?\s*(\d+:\d{2}:\d{2})", line)
    if m:
        return ExerciseSet(index=int(m.group(1)), duration_sec=_parse_time_to_seconds(m.group(2)))
    m = re.match(r"(\d+)\s*:?\s*(\d+)\s*$", line)
    if m:
        return ExerciseSet(index=int(m.group(1)), reps=int(m.group(2)))
    return None


class _LogPageParser(HTMLParser):
    """Best-effort parser for Jefit log pages (legacy fixedLogBar + generic fallbacks)."""

    def __init__(self) -> None:
        super().__init__()
        self.exercises: list[ScrapedExercise] = []
        self.meta: dict[str, str] = {}
        self._in_fixed = False
        self._block_i = -1
        self._blocks: list[str] = []
        self._capture = False
        self._buf: list[str] = []
        self._current_classes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        classes = ad.get("class", "").split()
        self._current_classes = classes
        if "fixedLogBar" in classes:
            self._in_fixed = True
            self._blocks = []
            self._block_i = -1
        if self._in_fixed and "fixedLogBarBlock" in classes:
            self._block_i += 1
            self._capture = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag in {"div", "span", "td"}:
            text = " ".join("".join(self._buf).split())
            while len(self._blocks) <= self._block_i:
                self._blocks.append("")
            # Keep the richest text for this block
            if len(text) >= len(self._blocks[self._block_i]):
                self._blocks[self._block_i] = text
            self._capture = False
            self._buf = []
        if tag == "div" and self._in_fixed and self._block_i >= 0 and not self._capture:
            # End of fixedLogBar roughly when we have blocks and leave
            pass

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)

    def close_exercise_if_ready(self) -> None:
        if not self._in_fixed:
            return
        # index 0 image, 1 name, 2 1RM, 3 sets
        if len(self._blocks) >= 2 and self._blocks[1]:
            ex = ScrapedExercise(name=self._blocks[1])
            if len(self._blocks) >= 3:
                try:
                    ex.one_rep_max = float(re.findall(r"[\d.]+", self._blocks[2])[0])
                except (IndexError, ValueError):
                    ex.one_rep_max = None
            if len(self._blocks) >= 4:
                set_text = self._blocks[3]
                matches = re.findall(r"\d+\s*:?\s*[\d.:xX]+", set_text)
                if not matches:
                    # also try split by newline-ish
                    matches = [p.strip() for p in re.split(r"[\n/|]+", set_text) if p.strip()]
                for i, part in enumerate(matches, start=1):
                    parsed = _parse_set_line(part if re.match(r"^\d", part) else f"{i}:{part}")
                    if parsed:
                        if parsed.index == 0:
                            parsed.index = i
                        ex.sets.append(parsed)
                        if parsed.weight is not None:
                            ex.exercise_type = "Lift"
                        elif parsed.duration_sec is not None:
                            ex.exercise_type = "Cardio"
                        else:
                            ex.exercise_type = "Bodyweight"
            if ex.name:
                self.exercises.append(ex)
        self._in_fixed = False
        self._blocks = []
        self._block_i = -1


def _parse_log_html(html: str, day: str, source: str) -> ScrapedWorkout:
    if "keep it private" in html.lower() or "don't have permission" in html.lower():
        raise JefitScrapeError(f"Log for {day} is private.")

    workout = ScrapedWorkout(date=day, source_url=source)

    # Meta fields from workout-session style labels
    for key, label in [
        ("sessionLength", r"Session\s*Length"),
        ("actualWorkout", r"Actual\s*Workout"),
        ("exercisesDone", r"Exercises?\s*Done"),
        ("weightLifted", r"Weight\s*Lifted"),
    ]:
        m = re.search(label + r".{0,80}?(\d+:\d{2}:\d{2}|\d+(?:\.\d+)?)", html, re.I | re.S)
        if not m:
            continue
        val = m.group(1)
        if ":" in val:
            secs = _parse_time_to_seconds(val)
            if key == "sessionLength":
                workout.session_length_sec = secs
            elif key == "actualWorkout":
                workout.actual_workout_sec = secs
        else:
            num = float(val)
            if key == "exercisesDone":
                workout.exercises_done = int(num)
            elif key == "weightLifted":
                workout.weight_lifted = num

    # Prefer legacy fixedLogBar blocks
    bars = re.findall(
        r'class="[^"]*fixedLogBar[^"]*"(.*?)</div>\s*</div>\s*</div>',
        html,
        re.I | re.S,
    )
    if bars:
        for block in re.finditer(
            r'<div[^>]*class="[^"]*fixedLogBar[^"]*"[^>]*>(.*?)</div>\s*(?=<div[^>]*class="[^"]*fixedLogBar|$)',
            html,
            re.I | re.S,
        ):
            chunk = block.group(1)
            texts = re.findall(r">([^<>]{1,200})<", chunk)
            texts = [" ".join(t.split()) for t in texts if t.strip()]
            # Heuristic: name is first meaningful non-empty alpha string; sets contain x
            name = None
            set_blob = None
            for t in texts:
                if name is None and re.search(r"[A-Za-z]{3,}", t) and "x" not in t.lower():
                    if not re.match(r"^[\d.:\s]+$", t):
                        name = t
                        continue
                if "x" in t.lower() or re.search(r"\d:\d", t):
                    set_blob = t
            if not name:
                continue
            ex = ScrapedExercise(name=name)
            if set_blob:
                for i, part in enumerate(re.findall(r"\d+\s*:?\s*[\d.]+x\d+|\d+\s*:?\s*\d+:\d{2}:\d{2}|\d+\s*:?\s*\d+", set_blob, re.I), start=1):
                    parsed = _parse_set_line(part)
                    if parsed:
                        if parsed.index <= 0:
                            parsed.index = i
                        ex.sets.append(parsed)
            workout.exercises.append(ex)

    if not workout.exercises:
        # Fallback: look for "NxR" patterns near exercise-like headings — low confidence
        for m in re.finditer(
            r"([A-Z][A-Za-z0-9 /()\-]{3,60}).{0,40}?((?:\d+(?:\.\d+)?x\d+[,\s]*){1,12})",
            html,
            re.S,
        ):
            name = " ".join(m.group(1).split())
            if any(bad in name.lower() for bad in ("http", "script", "cookie", "login")):
                continue
            ex = ScrapedExercise(name=name)
            for i, part in enumerate(re.findall(r"\d+(?:\.\d+)?x\d+", m.group(2)), start=1):
                parsed = _parse_set_line(f"{i}:{part}")
                if parsed:
                    ex.sets.append(parsed)
            if ex.sets:
                workout.exercises.append(ex)
            if len(workout.exercises) >= 20:
                break

    if workout.exercises_done is None:
        workout.exercises_done = len(workout.exercises)
    return workout


def fetch_single_date(user_id: str | int, day: date | str) -> ScrapedWorkout:
    if isinstance(day, date):
        day_s = day.isoformat()
    else:
        day_s = str(day)
        datetime.strptime(day_s, "%Y-%m-%d")  # validate
    url = log_url(user_id, day_s)
    html = _fetch(url)
    return _parse_log_html(html, day_s, url)


def fetch_most_recent(user_id: str | int, *, lookback_months: int = 2) -> ScrapedWorkout | None:
    today = date.today()
    y, m = today.year, today.month
    all_days: list[str] = []
    for _ in range(max(1, lookback_months)):
        all_days.extend(list_logged_dates(user_id, y, m))
        m -= 1
        if m <= 0:
            m = 12
            y -= 1
    if not all_days:
        return None
    latest = sorted(all_days)[-1]
    return fetch_single_date(user_id, latest)


def fetch_date_range(
    user_id: str | int,
    start: date,
    end: date,
) -> list[ScrapedWorkout]:
    """Fetch workouts for each day that has a calendar log between start and end inclusive."""
    if end < start:
        raise ValueError("end must be >= start")
    workouts: list[ScrapedWorkout] = []
    # Walk months covered
    cursor = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    logged: set[str] = set()
    while cursor <= end_month:
        for d in list_logged_dates(user_id, cursor.year, cursor.month):
            if start.isoformat() <= d <= end.isoformat():
                logged.add(d)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    for d in sorted(logged):
        try:
            workouts.append(fetch_single_date(user_id, d))
        except JefitScrapeError:
            continue
    return workouts


def fetch_recent_days(user_id: str | int, days: int = 7) -> list[ScrapedWorkout]:
    end = date.today()
    start = end - timedelta(days=max(0, days - 1))
    return fetch_date_range(user_id, start, end)

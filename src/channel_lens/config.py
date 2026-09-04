"""Application settings: where state lives, and what the user can configure.

Settings are a plain JSON file so they stay hand-editable when the UI is the
thing that's broken. Secrets live in the same file rather than a keyring —
this is a single-user local app, and a keyring dependency buys little when the
SQLite database sitting next to it is unencrypted anyway. The file is created
with owner-only permissions on POSIX; on Windows it inherits the user profile's
ACL, which is already user-scoped.

Nothing here talks to the network or the database, so it can be imported from
anywhere without a circular import.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

#: Environment variable that relocates all local state. Mainly for tests, which
#: point it at a tmp_path so a test run can never touch the real database.
HOME_ENV_VAR = "CHANNEL_LENS_HOME"

#: Settings keys whose values are never sent to the browser in full.
SECRET_KEYS = frozenset(
    {"youtube_api_key", "anthropic_api_key", "google_client_id", "google_client_secret"}
)


def app_home() -> Path:
    """Directory holding the database, settings, cached thumbnails, and logs.

    Honours ``CHANNEL_LENS_HOME`` first, then the platform's per-user data
    location. The directory is created on demand.
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        home = Path(override)
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        home = Path(base) / "channel-lens"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        home = Path(base) / "channel-lens"
    home.mkdir(parents=True, exist_ok=True)
    return home


def settings_path() -> Path:
    return app_home() / "settings.json"


def database_path() -> Path:
    return app_home() / "channel-lens.db"


def thumbnail_cache_dir() -> Path:
    d = app_home() / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def oauth_token_path() -> Path:
    return app_home() / "oauth_token.json"


@dataclass
class Settings:
    """User-configurable behaviour. Every field has a working default.

    The app must start and be navigable with none of these set — an unconfigured
    install shows what it needs and where to get it, rather than erroring out.
    """

    # --- Credentials -------------------------------------------------------
    #: YouTube Data API v3 key. Public data for any channel. Free, quota-limited.
    youtube_api_key: str = ""
    #: Anthropic API key, used only for thumbnail vision and title scoring.
    #: Everything else in the app works without it.
    anthropic_api_key: str = ""
    #: OAuth client for the YouTube Analytics API (your own channel's CTR,
    #: impressions, and retention). Comes from a Google Cloud "Desktop app"
    #: OAuth client — see docs/SETUP.md.
    google_client_id: str = ""
    google_client_secret: str = ""

    # --- Identity ----------------------------------------------------------
    #: Your own channel, so the app can separate "my videos" from competitors'.
    owned_channel_id: str = ""

    # --- Quota -------------------------------------------------------------
    #: Daily Data API budget. YouTube's default allowance is 10,000 units and
    #: resets at midnight US/Pacific. Defaulting below the ceiling leaves room
    #: for manual experiments outside the app, and means a runaway job trips our
    #: own guard before it trips Google's.
    daily_quota_budget: int = 9_000
    #: Refuse any single operation whose estimated cost exceeds this. Stops one
    #: careless "analyse this whole niche" click from eating the day.
    per_operation_quota_cap: int = 2_000

    # --- Outlier scoring ---------------------------------------------------
    #: How many of a channel's recent uploads form its performance baseline.
    baseline_window: int = 30
    #: Videos newer than this are excluded from a channel's *baseline* (they
    #: haven't finished accruing views, so including them drags the median
    #: down and inflates everyone else's multiplier). They are still scored.
    baseline_min_age_days: int = 14
    #: A channel with fewer than this many usable baseline videos gets its
    #: multipliers marked low-confidence rather than silently trusted.
    baseline_min_videos: int = 5
    #: Multiplier at or above which a video is called an outlier.
    outlier_threshold: float = 3.0

    # --- Tracking ----------------------------------------------------------
    #: Hours between automatic snapshots of watchlisted videos. Each snapshot
    #: costs 1 unit per 50 videos, so this is cheap even hourly; the default is
    #: conservative because velocity rarely needs finer resolution than this.
    tracker_interval_hours: int = 6
    #: Run the tracker automatically while the app is open.
    tracker_enabled: bool = True
    #: Stop snapshotting a tracked video once it is this old, to keep the
    #: watchlist's per-cycle cost from growing without bound.
    tracker_max_video_age_days: int = 120

    # --- Analysis ----------------------------------------------------------
    #: Claude model used for thumbnail vision and title scoring.
    anthropic_model: str = "claude-opus-5"
    #: Reasoning effort for analysis calls. Thumbnail and title analysis are
    #: high-volume structured-extraction tasks, which is the workload shape that
    #: gains least from deep reasoning — so this defaults low and the saving
    #: compounds over a few hundred thumbnails. Raise it if the write-ups feel thin.
    anthropic_effort: str = "low"
    #: Treat videos at or under this many seconds as Shorts. Shorts and
    #: long-form have completely different view distributions, so mixing them
    #: into one baseline produces nonsense multipliers.
    shorts_max_seconds: int = 180

    # --- Interface ---------------------------------------------------------
    #: "system", "light", or "dark".
    theme: str = "system"
    #: Port for the local server. 0 picks a free one.
    port: int = 8730
    #: Open a browser window on startup.
    open_browser: bool = True

    # Populated on load; not persisted.
    _unknown: dict[str, Any] = field(default_factory=dict, repr=False)

    # --- Persistence -------------------------------------------------------

    @classmethod
    def load(cls) -> "Settings":
        """Read settings from disk, falling back to defaults for anything absent.

        A corrupt or unreadable file is never fatal: the app starts on defaults
        and surfaces the problem, because a settings file you can't parse should
        not be the reason you can't reach the settings screen that fixes it.
        """
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        """Build from a dict, ignoring (but preserving) unrecognised keys.

        Unknown keys are kept so that opening this app with an older build,
        saving, and reopening with a newer one doesn't silently drop settings
        the older build didn't know about.

        >>> s = Settings.from_dict({"port": 9000, "future_option": True})
        >>> s.port
        9000
        >>> s._unknown
        {'future_option': True}
        """
        known = {f.name for f in fields(cls) if not f.name.startswith("_")}
        kwargs = {k: v for k, v in raw.items() if k in known}
        unknown = {k: v for k, v in raw.items() if k not in known}
        settings = cls(**kwargs)
        settings._unknown = unknown
        return settings

    def to_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        data.update(self._unknown)
        return data

    def save(self) -> None:
        """Write settings atomically, so a crash mid-write can't truncate them."""
        path = settings_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(path)

    # --- Presentation ------------------------------------------------------

    def redacted(self) -> dict[str, Any]:
        """Settings safe to hand to the frontend.

        Secrets collapse to a boolean ``*_set`` flag plus a masked hint, so the
        UI can show "configured" without the value ever leaving the process.

        >>> s = Settings(youtube_api_key="AIzaSyABCDEFGHIJKLMNOP")
        >>> r = s.redacted()
        >>> r["youtube_api_key_set"], r["youtube_api_key_hint"]
        (True, '…MNOP')
        >>> "youtube_api_key" in r
        False
        """
        data = self.to_dict()
        for key in SECRET_KEYS:
            value = data.pop(key, "") or ""
            data[f"{key}_set"] = bool(value)
            data[f"{key}_hint"] = f"…{value[-4:]}" if len(value) >= 4 else ""
        return data

    # --- Derived state -----------------------------------------------------

    @property
    def has_data_api(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def has_vision(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_oauth_client(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    def missing_requirements(self) -> list[dict[str, str]]:
        """Human-readable list of what isn't configured and what it unlocks.

        Drives the empty states, so an unconfigured app explains itself instead
        of just failing.
        """
        gaps: list[dict[str, str]] = []
        if not self.has_data_api:
            gaps.append(
                {
                    "key": "youtube_api_key",
                    "blocks": "Everything that reads public YouTube data: outliers, tracking, thumbnail and title analysis.",
                    "fix": "Create a YouTube Data API v3 key in Google Cloud, then paste it into Settings.",
                }
            )
        if not self.owned_channel_id:
            gaps.append(
                {
                    "key": "owned_channel_id",
                    "blocks": "The My Channel dashboard, and 'compare against my videos' on every other screen.",
                    "fix": "Paste your channel ID (or @handle) into Settings.",
                }
            )
        if not self.has_oauth_client:
            gaps.append(
                {
                    "key": "google_oauth",
                    "blocks": "Real CTR, impressions, and retention for your own videos. No external tool can supply these.",
                    "fix": "Create an OAuth 2.0 'Desktop app' client in the same Google Cloud project, then connect in Settings.",
                }
            )
        if not self.has_vision:
            gaps.append(
                {
                    "key": "anthropic_api_key",
                    "blocks": "AI thumbnail breakdowns and title curiosity scoring. Colour, text-area, and all deterministic title metrics still work without it.",
                    "fix": "Paste an Anthropic API key into Settings.",
                }
            )
        return gaps


_cached: Settings | None = None


def get_settings(refresh: bool = False) -> Settings:
    """Process-wide settings singleton.

    Cached because it is read on nearly every request; ``refresh=True`` after a
    save so the running app picks changes up without a restart.
    """
    global _cached
    if _cached is None or refresh:
        _cached = Settings.load()
    return _cached

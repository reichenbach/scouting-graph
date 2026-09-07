"""Column adapter for pitch tracking exports.

Different pitch tracking exports use different headers for the same
measurement. Rather than teach the pipeline about any one export, we keep a
mapping table from observed header names to a single canonical schema. A new
export shape is a few rows in ALIASES, not a code change anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


CANONICAL_COLUMNS = [
    "pitcher",
    "batter",
    "pitch_type",
    "velocity",
    "spin",
    "ivb",
    "hb",
    "plate_x",
    "plate_z",
    "count_balls",
    "count_strikes",
    "result",
    "batter_side",
    "pitcher_hand",
    "date",
]

# Columns a report cannot be built without.
REQUIRED_COLUMNS = [
    "pitcher",
    "pitch_type",
    "velocity",
    "plate_x",
    "plate_z",
    "count_balls",
    "count_strikes",
    "result",
    "batter_side",
    "date",
]

NUMERIC_COLUMNS = [
    "velocity",
    "spin",
    "ivb",
    "hb",
    "plate_x",
    "plate_z",
    "count_balls",
    "count_strikes",
]

# Header aliases, lowercased and stripped of spaces/underscores before lookup.
ALIASES: dict[str, str] = {}


def _register(canonical: str, *names: str) -> None:
    for name in names:
        ALIASES[_normalize(name)] = canonical


def _normalize(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


_register("pitcher", "pitcher", "pitcher_name", "pitchername", "player_pitcher", "pitcher_id")
_register("batter", "batter", "batter_name", "battername", "hitter")
_register(
    "pitch_type",
    "pitch_type",
    "pitchtype",
    "tagged_pitch_type",
    "auto_pitch_type",
    "pitch_name",
    "pitch",
)
_register(
    "velocity",
    "velocity",
    "velo",
    "rel_speed",
    "relspeed",
    "release_speed",
    "start_speed",
    "pitch_velocity",
    "mph",
)
_register("spin", "spin", "spin_rate", "spinrate", "release_spin_rate")
_register("ivb", "ivb", "induced_vert_break", "inducedvertbreak", "vertical_break_induced", "ivb_in")
_register("hb", "hb", "horz_break", "horizontal_break", "horzbreak", "hb_in")
_register("plate_x", "plate_x", "plate_side", "plate_loc_side", "platelocside", "px", "location_side")
_register(
    "plate_z",
    "plate_z",
    "plate_height",
    "plate_loc_height",
    "plateloc_height",
    "pz",
    "location_height",
)
_register("count_balls", "count_balls", "balls", "pre_balls", "balls_before")
_register("count_strikes", "count_strikes", "strikes", "pre_strikes", "strikes_before")
_register("result", "result", "pitch_result", "pitch_call", "pitchcall", "description", "outcome")
_register("batter_side", "batter_side", "batter_hand", "bat_side", "stand", "hitter_side")
_register("pitcher_hand", "pitcher_hand", "pitcher_throws", "throws", "p_throws", "pitcher_side")
_register("date", "date", "game_date", "gamedate", "played_on")


# Canonical pitch outcomes. Anything unrecognised stays as-is and is counted
# as neither a swing nor a take, which is visible in the fact counts.
RESULT_ALIASES: dict[str, str] = {}


def _register_result(canonical: str, *names: str) -> None:
    for name in names:
        RESULT_ALIASES[_normalize(name)] = canonical


_register_result("ball", "ball", "ball_called", "ballcalled", "called_ball", "blocked_ball")
_register_result("called_strike", "called_strike", "strike_called", "strikecalled", "called")
_register_result(
    "swinging_strike", "swinging_strike", "strike_swinging", "strikeswinging", "whiff", "swing_miss"
)
_register_result("foul", "foul", "foul_ball", "foulball", "foul_tip")
_register_result("in_play", "in_play", "inplay", "ball_in_play", "hit_into_play")
_register_result("hit_by_pitch", "hit_by_pitch", "hitbypitch", "hbp")

SWING_RESULTS = {"swinging_strike", "foul", "in_play"}
STRIKE_RESULTS = {"called_strike", "swinging_strike", "foul", "in_play"}


@dataclass
class AdapterReport:
    """What the adapter did, so the operator can see it in the audit trail."""

    mapping: dict[str, str] = field(default_factory=dict)
    unmapped_headers: list[str] = field(default_factory=list)
    missing_canonical: list[str] = field(default_factory=list)


def adapt(df: pd.DataFrame) -> tuple[pd.DataFrame, AdapterReport]:
    """Rename an arbitrary export into the canonical schema.

    Does not validate; that is the gates' job. This only renames and
    normalises, so that validate() has one shape to reason about.
    """
    report = AdapterReport()
    rename: dict[str, str] = {}
    for column in df.columns:
        canonical = ALIASES.get(_normalize(column))
        if canonical is None:
            report.unmapped_headers.append(str(column))
            continue
        if canonical in rename.values():
            # A duplicate mapping. Keep the first and report the second.
            report.unmapped_headers.append(str(column))
            continue
        rename[column] = canonical
        report.mapping[str(column)] = canonical

    out = df.rename(columns=rename)
    out = out[[c for c in CANONICAL_COLUMNS if c in out.columns]].copy()

    for column in NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")

    if "result" in out.columns:
        out["result"] = out["result"].map(
            lambda v: RESULT_ALIASES.get(_normalize(v), str(v).strip().lower())
        )
    for column in ("batter_side", "pitcher_hand"):
        if column in out.columns:
            out[column] = out[column].astype(str).str.strip().str.upper().str[:1]
    if "pitcher" in out.columns:
        out["pitcher"] = out["pitcher"].astype(str).str.strip()
    if "pitch_type" in out.columns:
        out["pitch_type"] = out["pitch_type"].astype(str).str.strip()

    report.missing_canonical = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    return out, report

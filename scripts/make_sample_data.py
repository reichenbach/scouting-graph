"""Generate the synthetic sample data. Seeded, so it is reproducible.

Nothing in here came from a real team, a real player, or a real export. The
pitch sequences are simulated plate appearances so that counts, first pitch
strike rates and chase rates behave the way real ones do.

    python scripts/make_sample_data.py
"""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_data"
INBOX = ROOT / "inbox"

PITCH_MIX = {
    "Fastball": {"velo": (90.5, 1.5), "spin": (2230, 110), "ivb": (15.0, 2.0), "hb": (9.0, 3.0)},
    "Slider": {"velo": (81.0, 1.6), "spin": (2380, 130), "ivb": (2.0, 2.5), "hb": (-6.0, 3.0)},
    "Changeup": {"velo": (82.5, 1.4), "spin": (1750, 120), "ivb": (9.0, 2.5), "hb": (12.0, 3.0)},
    "Curveball": {"velo": (75.5, 1.8), "spin": (2480, 140), "ivb": (-6.0, 3.0), "hb": (-9.0, 3.5)},
}

ZONE_HALF_WIDTH = 0.83
ZONE_BOTTOM = 1.50
ZONE_TOP = 3.50


def count_state(balls: int, strikes: int) -> str:
    if strikes >= 2:
        return "two_strike"
    if balls > strikes:
        return "behind"
    if strikes > balls:
        return "ahead"
    return "even"


def choose_pitch(rng: random.Random, profile: dict, state: str) -> str:
    weights = profile["mix"][state]
    types = list(weights)
    return rng.choices(types, weights=[weights[t] for t in types], k=1)[0]


def in_zone(x: float, z: float) -> bool:
    return abs(x) <= ZONE_HALF_WIDTH and ZONE_BOTTOM <= z <= ZONE_TOP


def sample_location(rng: random.Random, profile: dict, pitch_type: str, state: str):
    bias_x, bias_z, spread = profile["location"][pitch_type]
    # Pitchers fill up the zone when behind and expand when ahead.
    pull = {"behind": 0.55, "even": 0.8, "ahead": 1.15, "two_strike": 1.25}[state]
    x = rng.gauss(bias_x, spread * pull)
    z = rng.gauss(bias_z, spread * pull)
    return round(x, 3), round(z, 3)


def sample_result(rng: random.Random, pitch_type: str, zone: bool, state: str) -> str:
    if zone:
        swing_p = {"behind": 0.52, "even": 0.60, "ahead": 0.66, "two_strike": 0.78}[state]
    else:
        swing_p = {"behind": 0.14, "even": 0.22, "ahead": 0.30, "two_strike": 0.38}[state]
        if pitch_type in ("Slider", "Curveball"):
            swing_p += 0.08
    if rng.random() < 0.006:
        return "HitByPitch"
    if rng.random() >= swing_p:
        return "StrikeCalled" if zone else "BallCalled"
    whiff_p = {"Fastball": 0.18, "Slider": 0.33, "Changeup": 0.28, "Curveball": 0.30}[pitch_type]
    if not zone:
        whiff_p += 0.15
    roll = rng.random()
    if roll < whiff_p:
        return "StrikeSwinging"
    if roll < whiff_p + 0.38:
        return "FoulBall"
    return "InPlay"


def simulate(rng: random.Random, profile: dict, dates: list[str], target_pitches: int) -> list[dict]:
    rows: list[dict] = []
    per_date = target_pitches // len(dates)
    for date in dates:
        thrown = 0
        while thrown < per_date:
            side = "R" if rng.random() < 0.62 else "L"
            batter = f"H{rng.randint(1, 9):02d}"
            balls = strikes = 0
            while True:
                state = count_state(balls, strikes)
                pitch_type = choose_pitch(rng, profile, state)
                spec = PITCH_MIX[pitch_type]
                x, z = sample_location(rng, profile, pitch_type, state)
                zone = in_zone(x, z)
                result = sample_result(rng, pitch_type, zone, state)
                rows.append(
                    {
                        "pitcher": profile["name"],
                        "batter": batter,
                        "pitch_type": pitch_type,
                        "velocity": round(rng.gauss(*spec["velo"]), 1),
                        "spin": int(rng.gauss(*spec["spin"])),
                        "ivb": round(rng.gauss(*spec["ivb"]), 1),
                        "hb": round(rng.gauss(*spec["hb"]), 1),
                        "plate_x": x,
                        "plate_z": z,
                        "count_balls": balls,
                        "count_strikes": strikes,
                        "result": result,
                        "batter_side": side,
                        "pitcher_hand": profile["hand"],
                        "date": date,
                    }
                )
                thrown += 1
                if result == "BallCalled":
                    balls += 1
                elif result in ("StrikeCalled", "StrikeSwinging"):
                    strikes += 1
                elif result == "FoulBall" and strikes < 2:
                    strikes += 1
                if result in ("InPlay", "HitByPitch") or balls >= 4 or strikes >= 3:
                    break
    return rows


def profile(name: str, hand: str, style: str) -> dict:
    if style == "power":
        mix = {
            "even": {"Fastball": 0.62, "Slider": 0.22, "Changeup": 0.10, "Curveball": 0.06},
            "ahead": {"Fastball": 0.42, "Slider": 0.36, "Changeup": 0.13, "Curveball": 0.09},
            "behind": {"Fastball": 0.80, "Slider": 0.12, "Changeup": 0.06, "Curveball": 0.02},
            "two_strike": {"Fastball": 0.34, "Slider": 0.42, "Changeup": 0.14, "Curveball": 0.10},
        }
        location = {
            "Fastball": (0.05, 2.65, 0.95),
            "Slider": (-0.30, 2.05, 1.05),
            "Changeup": (0.25, 2.05, 1.02),
            "Curveball": (-0.10, 2.25, 1.12),
        }
    else:
        mix = {
            "even": {"Fastball": 0.48, "Slider": 0.18, "Changeup": 0.22, "Curveball": 0.12},
            "ahead": {"Fastball": 0.34, "Slider": 0.24, "Changeup": 0.24, "Curveball": 0.18},
            "behind": {"Fastball": 0.66, "Slider": 0.12, "Changeup": 0.16, "Curveball": 0.06},
            "two_strike": {"Fastball": 0.30, "Slider": 0.28, "Changeup": 0.26, "Curveball": 0.16},
        }
        location = {
            "Fastball": (-0.05, 2.55, 0.92),
            "Slider": (0.28, 2.10, 1.03),
            "Changeup": (-0.28, 2.00, 1.00),
            "Curveball": (0.05, 2.30, 1.10),
        }
    return {"name": name, "hand": hand, "mix": mix, "location": location}


ALT_HEADERS = {
    "pitcher": "Pitcher",
    "batter": "Batter",
    "pitch_type": "TaggedPitchType",
    "velocity": "RelSpeed",
    "spin": "SpinRate",
    "ivb": "InducedVertBreak",
    "hb": "HorzBreak",
    "plate_x": "PlateLocSide",
    "plate_z": "PlateLocHeight",
    "count_balls": "Balls",
    "count_strikes": "Strikes",
    "result": "PitchCall",
    "batter_side": "BatterSide",
    "pitcher_hand": "PitcherThrows",
    "date": "Date",
}

TREES = [
    "Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Gum", "Hawthorn",
    "Ironwood", "Juniper", "Katsura", "Larch", "Maple", "Nyssa", "Oak",
    "Poplar", "Quince", "Redbud", "Spruce", "Tupelo",
]

POSITIONS = [
    "C", "1B", "2B/SS", "SS/3B", "3B/1B", "SS/2B/3B", "LF/CF", "CF", "RF/LF",
    "2B/OF", "C/1B", "SS", "OF", "1B/OF", "3B/SS", "2B", "CF/RF", "C/3B",
    "SS/OF", "1B/3B",
]


def main() -> None:
    SAMPLE.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)

    rng = random.Random(20260907)
    dates_a = ["2026-09-04", "2026-09-05", "2026-09-06"]
    dates_b = ["2026-08-28", "2026-08-29", "2026-08-30"]

    rows_a = simulate(rng, profile("OPP-11", "R", "power"), dates_a, 300)
    rows_a += simulate(rng, profile("OPP-24", "L", "mixed"), dates_a, 300)
    frame_a = pd.DataFrame(rows_a)
    frame_a.to_csv(SAMPLE / "pitch_export_a.csv", index=False)
    frame_a.to_csv(INBOX / "sample.csv", index=False)

    rows_b = simulate(rng, profile("OPP-32", "R", "mixed"), dates_b, 300)
    rows_b += simulate(rng, profile("OPP-47", "R", "power"), dates_b, 300)
    frame_b = pd.DataFrame(rows_b).rename(columns=ALT_HEADERS)
    frame_b.to_csv(SAMPLE / "pitch_export_b.csv", index=False)

    # Bad schema: the location and velocity columns never made it into the export.
    bad = frame_a.drop(columns=["velocity", "plate_x", "plate_z"]).head(400)
    bad.to_csv(SAMPLE / "bad_schema.csv", index=False)

    # Thin coverage: one game, not enough pitches to say anything honest.
    thin = frame_a[frame_a["date"] == dates_a[0]].head(55)
    thin.to_csv(SAMPLE / "thin_coverage.csv", index=False)

    # Roster
    roster_rng = random.Random(11)
    roster = []
    for index, surname in enumerate(TREES):
        roster.append(
            {
                "player": surname,
                "positions_played": POSITIONS[index],
                "bats": roster_rng.choice(["R", "R", "L", "S"]),
                "throws": roster_rng.choice(["R", "R", "R", "L"]),
                "notes": roster_rng.choice(
                    [
                        "everyday starter",
                        "platoon bat",
                        "defensive replacement",
                        "freshman, limited reps",
                        "took reps at a second spot in fall",
                        "",
                    ]
                ),
            }
        )
    pd.DataFrame(roster).to_csv(SAMPLE / "roster.csv", index=False)

    schedule = [
        {"date": "2026-09-11", "opponent": "Opponent A", "site": "home", "travel": "none"},
        {"date": "2026-09-12", "opponent": "Opponent A", "site": "home", "travel": "none"},
        {"date": "2026-09-13", "opponent": "Opponent A", "site": "home", "travel": "none"},
        {"date": "2026-09-18", "opponent": "Opponent B", "site": "away", "travel": "bus, 3 hours"},
        {"date": "2026-09-19", "opponent": "Opponent B", "site": "away", "travel": "bus, 3 hours"},
        {"date": "2026-09-25", "opponent": "Opponent C", "site": "neutral", "travel": "bus, 5 hours"},
        {"date": "2026-09-26", "opponent": "Opponent C", "site": "neutral", "travel": "bus, 5 hours"},
    ]
    pd.DataFrame(schedule).to_csv(SAMPLE / "schedule.csv", index=False)

    print(f"wrote sample data to {SAMPLE}")


if __name__ == "__main__":
    main()

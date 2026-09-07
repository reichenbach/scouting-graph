"""The facts sheet. Pure Python and pandas. No model touches this file.

Every number that can appear in a report is computed here, given an id, and
stamped with the sample size it came from. The model downstream is handed
this sheet and is allowed to write sentences about it. It is not allowed to
produce a number of its own, and check_notes proves that after the fact.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from . import config
from .adapters import STRIKE_RESULTS, SWING_RESULTS


def file_provenance(path: Path, df: pd.DataFrame) -> dict:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    dates = sorted({str(d) for d in df["date"].dropna().unique()})
    return {
        "source_file": Path(path).name,
        "sha256": digest,
        "rows": int(len(df)),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "games": len(dates),
    }


def _in_zone(row_x: float, row_z: float) -> bool:
    if pd.isna(row_x) or pd.isna(row_z):
        return False
    return (
        abs(row_x) <= config.ZONE_HALF_WIDTH
        and config.ZONE_BOTTOM <= row_z <= config.ZONE_TOP
    )


def _zone_mask(df: pd.DataFrame) -> pd.Series:
    return (
        df["plate_x"].abs().le(config.ZONE_HALF_WIDTH)
        & df["plate_z"].between(config.ZONE_BOTTOM, config.ZONE_TOP)
    ).fillna(False)


def _count_state(balls: float, strikes: float) -> str:
    if pd.isna(balls) or pd.isna(strikes):
        return "unknown"
    if strikes >= 2:
        return "two_strike"
    if balls > strikes:
        return "behind"
    if strikes > balls:
        return "ahead"
    return "even"


def _grid_cell(x: float, z: float) -> str | None:
    """3x3 cell label for an in-zone pitch, from the pitcher's view."""
    if not _in_zone(x, z):
        return None
    third = (2 * config.ZONE_HALF_WIDTH) / 3.0
    col_idx = min(2, int((x + config.ZONE_HALF_WIDTH) / third))
    height = (config.ZONE_TOP - config.ZONE_BOTTOM) / 3.0
    row_idx = min(2, int((z - config.ZONE_BOTTOM) / height))
    cols = ["left", "middle", "right"]
    rows = ["low", "middle", "up"]
    return f"{rows[row_idx]}-{cols[col_idx]}"


class _FactBook:
    def __init__(self) -> None:
        self._facts: dict[str, dict] = {}
        self._n = 0

    def add(
        self,
        pitcher: str,
        metric: str,
        label: str,
        value: float,
        n: int,
        unit: str = "percent",
    ) -> str:
        self._n += 1
        fact_id = f"f{self._n}"
        self._facts[fact_id] = {
            "id": fact_id,
            "pitcher": pitcher,
            "metric": metric,
            "label": label,
            "value": round(float(value), 1),
            "n": int(n),
            "unit": unit,
        }
        return fact_id

    @property
    def facts(self) -> dict[str, dict]:
        return self._facts


def compute_facts(df: pd.DataFrame, provenance: dict) -> dict:
    """Build the facts sheet for every pitcher in the frame."""
    book = _FactBook()
    work = df.copy()
    work["in_zone"] = _zone_mask(work)
    work["is_swing"] = work["result"].isin(SWING_RESULTS)
    work["is_strike"] = work["result"].isin(STRIKE_RESULTS)
    work["count_state"] = [
        _count_state(b, s) for b, s in zip(work["count_balls"], work["count_strikes"])
    ]
    work["grid_cell"] = [
        _grid_cell(x, z) for x, z in zip(work["plate_x"], work["plate_z"])
    ]

    pitchers: dict[str, dict] = {}

    for pitcher, pdf in work.groupby("pitcher", sort=True):
        total = len(pdf)
        fact_ids: list[str] = []

        # Usage by pitch type, overall.
        usage = pdf.groupby("pitch_type").size().sort_values(ascending=False)
        primary_types = [t for t, n in usage.items() if n / total >= 0.05]
        for pitch_type, n in usage.items():
            fact_ids.append(
                book.add(
                    pitcher,
                    f"usage.{pitch_type}",
                    f"{pitch_type} usage overall",
                    n / total * 100.0,
                    int(total),
                )
            )

        # Usage by batter side.
        for side, sdf in pdf.groupby("batter_side"):
            if len(sdf) == 0:
                continue
            side_label = {"R": "right handed hitters", "L": "left handed hitters"}.get(
                side, f"{side} handed hitters"
            )
            for pitch_type in primary_types:
                n_type = int((sdf["pitch_type"] == pitch_type).sum())
                fact_ids.append(
                    book.add(
                        pitcher,
                        f"usage.{pitch_type}.vs_{side}",
                        f"{pitch_type} usage against {side_label}",
                        n_type / len(sdf) * 100.0,
                        int(len(sdf)),
                    )
                )

        # Usage by count state.
        for state in ("ahead", "even", "behind", "two_strike"):
            sdf = pdf[pdf["count_state"] == state]
            if len(sdf) == 0:
                continue
            state_label = state.replace("_", " ")
            for pitch_type in primary_types:
                n_type = int((sdf["pitch_type"] == pitch_type).sum())
                fact_ids.append(
                    book.add(
                        pitcher,
                        f"usage.{pitch_type}.count_{state}",
                        f"{pitch_type} usage when {state_label}",
                        n_type / len(sdf) * 100.0,
                        int(len(sdf)),
                    )
                )

        # Velocity per pitch type.
        for pitch_type in usage.index:
            velo = pdf.loc[pdf["pitch_type"] == pitch_type, "velocity"].dropna()
            if len(velo) == 0:
                continue
            fact_ids.append(
                book.add(
                    pitcher,
                    f"velocity.mean.{pitch_type}",
                    f"{pitch_type} average velocity",
                    float(velo.mean()),
                    int(len(velo)),
                    unit="mph",
                )
            )
            fact_ids.append(
                book.add(
                    pitcher,
                    f"velocity.max.{pitch_type}",
                    f"{pitch_type} top velocity",
                    float(velo.max()),
                    int(len(velo)),
                    unit="mph",
                )
            )

        # Zone location tendency, 3x3, share of in-zone pitches.
        in_zone = pdf[pdf["in_zone"]]
        if len(in_zone) > 0:
            cell_counts = in_zone["grid_cell"].value_counts()
            for cell in [
                "up-left", "up-middle", "up-right",
                "middle-left", "middle-middle", "middle-right",
                "low-left", "low-middle", "low-right",
            ]:
                fact_ids.append(
                    book.add(
                        pitcher,
                        f"zone.{cell}",
                        f"share of in-zone pitches in the {cell.replace('-', ' ')} cell",
                        int(cell_counts.get(cell, 0)) / len(in_zone) * 100.0,
                        int(len(in_zone)),
                    )
                )
        fact_ids.append(
            book.add(
                pitcher,
                "zone.rate",
                "share of all pitches in the strike zone",
                float(pdf["in_zone"].mean() * 100.0),
                int(total),
            )
        )

        # First pitch strike rate.
        first = pdf[(pdf["count_balls"] == 0) & (pdf["count_strikes"] == 0)]
        if len(first) > 0:
            fact_ids.append(
                book.add(
                    pitcher,
                    "first_pitch_strike_rate",
                    "first pitch strike rate",
                    float(first["is_strike"].mean() * 100.0),
                    int(len(first)),
                )
            )

        # Whiff rate per pitch type.
        for pitch_type in usage.index:
            tdf = pdf[pdf["pitch_type"] == pitch_type]
            swings = int(tdf["is_swing"].sum())
            if swings == 0:
                continue
            whiffs = int((tdf["result"] == "swinging_strike").sum())
            fact_ids.append(
                book.add(
                    pitcher,
                    f"whiff_rate.{pitch_type}",
                    f"{pitch_type} whiff rate per swing",
                    whiffs / swings * 100.0,
                    swings,
                )
            )

        # Chase rate: swings at pitches out of the zone.
        out_zone = pdf[~pdf["in_zone"]]
        if len(out_zone) > 0:
            fact_ids.append(
                book.add(
                    pitcher,
                    "chase_rate",
                    "chase rate on pitches out of the zone",
                    float(out_zone["is_swing"].mean() * 100.0),
                    int(len(out_zone)),
                )
            )

        pitchers[str(pitcher)] = {
            "pitcher": str(pitcher),
            "total_pitches": int(total),
            "hand": str(pdf["pitcher_hand"].mode().iloc[0]) if "pitcher_hand" in pdf and len(pdf["pitcher_hand"].mode()) else "",
            "fact_ids": fact_ids,
        }

    return {
        "version": config.PIPELINE_VERSION,
        "schema": config.FACTS_SCHEMA_VERSION,
        "rules_version": config.RULES_VERSION,
        "provenance": provenance,
        "pitchers": pitchers,
        "facts": book.facts,
    }


def facts_for(facts_sheet: dict, pitcher: str) -> list[dict]:
    ids = facts_sheet["pitchers"][pitcher]["fact_ids"]
    return [facts_sheet["facts"][i] for i in ids]


def render_fact_lines(facts_sheet: dict, pitcher: str) -> str:
    """The exact text handed to the model. Nothing else is."""
    lines = []
    for fact in facts_for(facts_sheet, pitcher):
        unit = "%" if fact["unit"] == "percent" else f" {fact['unit']}"
        lines.append(f"[{fact['id']}] {fact['label']}: {fact['value']}{unit} (n={fact['n']})")
    return "\n".join(lines)

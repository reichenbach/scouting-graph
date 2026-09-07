"""Deterministic gates. They block. They never guess.

Each failure carries two texts on purpose:

  operator_detail - loud, specific, actionable, for whoever runs the pipeline
  coach_note      - plain and kind, for the person waiting on the report

A gate failure is a normal outcome, not an exception. Nothing downstream of a
failed gate is allowed to run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from . import config
from .adapters import AdapterReport, NUMERIC_COLUMNS, REQUIRED_COLUMNS


@dataclass
class GateFailure:
    gate: str
    code: str
    operator_detail: str
    coach_note: str

    def to_dict(self) -> dict:
        return asdict(self)


def schema_gate(df: pd.DataFrame, adapter_report: AdapterReport) -> list[GateFailure]:
    failures: list[GateFailure] = []

    missing = list(adapter_report.missing_canonical)
    if missing:
        failures.append(
            GateFailure(
                gate="schema",
                code="missing_columns",
                operator_detail=(
                    "Required columns absent after adapter mapping: "
                    + ", ".join(missing)
                    + ". Headers seen but not recognised: "
                    + (", ".join(adapter_report.unmapped_headers) or "none")
                    + ". Add the header to ALIASES in adapters.py if this export is legitimate."
                ),
                coach_note=(
                    "This file is missing information the report needs: "
                    + ", ".join(missing)
                    + ". Please re-export the pitch tracking file with those fields included."
                ),
            )
        )
        # Without the required columns the remaining checks are meaningless.
        return failures

    if len(df) == 0:
        failures.append(
            GateFailure(
                gate="schema",
                code="empty_file",
                operator_detail="File parsed to zero rows.",
                coach_note="This file came through empty. Nothing was pitched in it that we can read.",
            )
        )
        return failures

    for column in NUMERIC_COLUMNS:
        if column not in df.columns:
            continue
        null_pct = float(df[column].isna().mean() * 100.0)
        if null_pct == 100.0:
            failures.append(
                GateFailure(
                    gate="schema",
                    code=f"unparseable_{column}",
                    operator_detail=(
                        f"Column '{column}' is present but no value parsed as a number "
                        f"(100 percent null after coercion)."
                    ),
                    coach_note=(
                        f"The {column.replace('_', ' ')} column came through as text we could not read. "
                        "Please re-export without formatting applied to that column."
                    ),
                )
            )

    for column in REQUIRED_COLUMNS:
        if column in ("pitcher", "pitch_type", "result", "batter_side", "date"):
            if df[column].isna().all() or (df[column].astype(str).str.strip() == "").all():
                failures.append(
                    GateFailure(
                        gate="schema",
                        code=f"empty_{column}",
                        operator_detail=f"Column '{column}' is present but entirely blank.",
                        coach_note=(
                            f"Every row is missing {column.replace('_', ' ')}. "
                            "The export looks incomplete."
                        ),
                    )
                )
    return failures


def coverage_gate(df: pd.DataFrame) -> list[GateFailure]:
    failures: list[GateFailure] = []

    games = sorted({str(d) for d in df["date"].dropna().unique()})
    if len(games) < config.MIN_GAMES:
        failures.append(
            GateFailure(
                gate="coverage",
                code="too_few_games",
                operator_detail=(
                    f"{len(games)} distinct game date(s) in file; minimum is {config.MIN_GAMES}. "
                    f"Dates seen: {', '.join(games) or 'none'}."
                ),
                coach_note=(
                    f"There is only {len(games)} game of data here. We hold a report until there are at "
                    f"least {config.MIN_GAMES}, because one outing does not describe how someone pitches."
                ),
            )
        )

    counts = df.groupby("pitcher").size().sort_values(ascending=False)
    thin = counts[counts < config.MIN_PITCHES_PER_PITCHER]
    if len(thin) > 0:
        detail = ", ".join(f"{name}={int(n)}" for name, n in thin.items())
        failures.append(
            GateFailure(
                gate="coverage",
                code="too_few_pitches",
                operator_detail=(
                    f"Pitchers under the {config.MIN_PITCHES_PER_PITCHER} pitch minimum: {detail}."
                ),
                coach_note=(
                    "Some pitchers in this file do not have enough pitches for us to say anything "
                    f"honest about them yet. We need at least {config.MIN_PITCHES_PER_PITCHER} pitches "
                    "per pitcher."
                ),
            )
        )

    location_null_pct = float(
        (df["plate_x"].isna() | df["plate_z"].isna()).mean() * 100.0
    )
    if location_null_pct > config.MAX_NULL_LOCATION_PCT:
        failures.append(
            GateFailure(
                gate="coverage",
                code="missing_locations",
                operator_detail=(
                    f"{location_null_pct:.1f} percent of rows have no plate location; "
                    f"ceiling is {config.MAX_NULL_LOCATION_PCT:.0f} percent."
                ),
                coach_note=(
                    f"About {location_null_pct:.0f} percent of the pitches in this file have no location "
                    "recorded, so the zone and chase numbers would be misleading. Holding the report."
                ),
            )
        )
    return failures


def sanity_gate(df: pd.DataFrame) -> list[GateFailure]:
    failures: list[GateFailure] = []

    velocity = df["velocity"].dropna()
    if len(velocity) == 0:
        return failures

    out_of_range = velocity[(velocity < config.VELOCITY_MIN) | (velocity > config.VELOCITY_MAX)]
    pct = float(len(out_of_range) / len(velocity) * 100.0)
    if pct > config.MAX_VELOCITY_OUTLIER_PCT:
        failures.append(
            GateFailure(
                gate="sanity",
                code="velocity_out_of_range",
                operator_detail=(
                    f"{len(out_of_range)} of {len(velocity)} velocity values ({pct:.1f} percent) fall "
                    f"outside {config.VELOCITY_MIN:.0f}-{config.VELOCITY_MAX:.0f} mph. "
                    f"Observed range {velocity.min():.1f} to {velocity.max():.1f}. "
                    "Check the units on the export."
                ),
                coach_note=(
                    "The velocity readings in this file are outside anything we can treat as real, "
                    "so the report is on hold. This usually means the export used different units."
                ),
            )
        )
    return failures


def run_all_gates(df: pd.DataFrame, adapter_report: AdapterReport) -> list[GateFailure]:
    failures = schema_gate(df, adapter_report)
    if failures:
        # Coverage and sanity assume the schema holds.
        return failures
    failures.extend(coverage_gate(df))
    failures.extend(sanity_gate(df))
    return failures

# How a report number is defined

Synthetic. This is the methodology a staff would read, not a live export.

## Facts sheet

Every number that can appear on a report is computed in code before a model
writes a word. The model is handed a facts sheet and never sees the raw
export. Each fact has an id such as f12, a value, and an n. A fact without
an n does not go on a page.

## Chase rate

Chase rate is the share of pitches outside the strike zone that the hitter
swung at. The zone is 0.83 feet either side of the middle and from 1.50 feet
to 3.50 feet off the ground. A swing at a pitch inside that box is not a
chase. The n for chase rate is the number of pitches thrown outside the zone,
not the number of swings.

## Coverage gates

A pitcher page is not produced from a thin sample. The file must contain at
least 100 pitches for that pitcher and at least 2 games. A failure holds the
whole report. There is no partial page, and nothing is imputed to fill a gap.

## What the model may write

The model writes 4 to 7 sentences from the facts sheet. It may round a number
to a whole number. It may not compute, combine, average, or infer a new
number. Every sentence that contains a number cites the fact id in square
brackets.

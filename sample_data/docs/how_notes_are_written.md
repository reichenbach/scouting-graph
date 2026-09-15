# How a scouting note is written

Synthetic. The contract the notes step has to satisfy, written out so a
staff member can read it without opening the code.

## Shape

One page per pitcher. The notes are 4 to 7 sentences. Five is the safe
middle. No praise, no projection about the next start, no comparison to a
teammate by name. Describe what the sample shows and let the staff decide.

## Citations

Every number in the notes carries a fact id in square brackets, like [f12].
The id has to belong to that pitcher. A number that does not match a cited
fact holds the run. The check is code, not a request in the prompt.

## Retry and hold

A draft that fails the numeric check is sent back once with the reasons. A
second failure holds the report. Nothing is delivered from a held run. A
person still has to approve a draft that passed the check, and that pause is
a real interrupt, not a log line.

## Provenance

The PDF footer carries the pipeline version, the source filename, and the
sha256 of the file. A report without those is not a report from this
pipeline.

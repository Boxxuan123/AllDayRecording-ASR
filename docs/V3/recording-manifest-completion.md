# Recording manifest completion

The Phone uploads `AllDayRecording session manifest v2` after the referenced WAV
files have upload receipts. `completion` is optional. An absent `completion`
means the current audio input passed continuity, size, and SHA-256 checks but
the end of the recording has **not** been verified. It must not be interpreted
as `watch_stop` or `legacy_user_confirmed`.

When present, `completion.source` is either `watch_stop` or
`legacy_user_confirmed`, with matching `completedSegments` and `totalSamples`
and a positive `confirmedAt`. A changed completion for identical audio records
confirmation evidence without changing `input_revision` or rerunning a
successful processing job. A continuous appended tail changes the audio input
and creates the next revision; the original manifest remains immutable.

New Phone recordings wait for the Watch stop record before submission. The
optional completion path exists for recordings predating the compatibility
boundary, whose missing stop record cannot be reconstructed as proof.

# Lumora Experimentation Guidelines

## Analysis standards
The primary metric for checkout and pricing experiments is conversion: the share of assigned users with at
least one conversion event. Report the control rate, the treatment rate and the relative lift
(treatment rate divided by control rate, minus one).

## Significance
A result is considered statistically significant at p < 0.05 (two-sided). Do not ship on non-significant results.

## Sample ratio mismatch (SRM)
Before reading any result, check the sample ratio. If the observed treatment share deviates from the intended
share such that a chi-square test gives p < 0.001, the experiment has a sample ratio mismatch. An experiment
with SRM must not be trusted: its results are invalid and the assignment pipeline must be investigated.
As a quick rule, with a few thousand users a deviation of more than 2 percentage points from a 50/50 split is
almost always an SRM.

## Minimum sample
Each variant needs at least 1000 assigned users before analysis.

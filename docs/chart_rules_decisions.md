# Chart rules: open decisions

Decisions about [chart_rules.md](chart_rules.md) that are parked for later (raised 2026-09-26). This file is
not read by the reviewer; only `chart_rules.md` is.

Context: in the first test with the rules, the reviewer applied F1 (terminal value 37x the next largest,
excluded from view) and F2 (trimmed 11 leading empty quarters) correctly, but broke N3 (wrote "3691698.04"),
F5 (also capped the y-axis, which wasn't needed once the outlier was out of view) and A3 (no units line).

1. **Outlier threshold (F1).** Currently 5x the next-largest absolute value. The typical case (a terminal
   value) is ~37x, so anything from 3x to 10x catches it. Tighter or looser?

2. **Enforce some rules in code instead of trusting the model?** Proposal: after every review, in
   `bench/chartreview.py` (like the title-year fix already there):
   - format numbers in notes (N3)
   - drop a y-axis cap when the visible range already excludes the outlier (F5)
   - always add "Units aren't labelled in the workbook." when no units are given (A3)
   The model keeps the judgement calls.

3. **Should the chat model also follow the rules?** Today only the reviewer reads them. Giving them to the
   chat model too would mean better series and chart-type choices up front and less for the reviewer to fix;
   cost is about 1,200 extra tokens per question that draws a chart.

4. **Anything missing?** Candidates:
   - state the model's sign convention (in the reference model, negatives are payments out)
   - a maximum number of series per chart
   - prefer annual over quarterly values for long timelines (needs a new ability to aggregate periods;
     the reviewer can't do this today)

5. **Location.** `docs/` is in the public GitHub repo. The rules are generic (no client details). Keep them
   there, or move them somewhere private?

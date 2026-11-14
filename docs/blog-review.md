# Sentinel blog local review

Prepared November 13, 2026. Publication remains pending author review.

Preview: http://localhost:8765/BLOG_POST.html

## Evidence and validation

- Current repository suite: `python3 -m pytest` — **65 passed**, 99.56 seconds. The README badge says 66; the article avoids repeating that stale count.
- Offline reader command: `sentinel scan samples/parse_duration.py::parse_duration --probe "('2h',)"` — status OK, value -3600.
- Historical primary campaign: `results_holdout/bench_details.json`, generated August 24, 2026. Embedded explorer contains all 39 primary mutation records: 32 catches and seven misses.
- Three primary seeds each total 32/39; disjoint mutation holdout totals 35/42. These are archived results, not a new model campaign.
- Successful baseline evidence: `results_baseline/bench_details.json`. Valid clean-reference comparison: 11/13 versus 11/13. Three seeded originals have invalid baseline suites.
- Ten original findings are broken down explicitly; no zero-false-positive claim is made.
- Parser probes in the article were directly checked. The probe interaction is illustrative, not a saved minimizer trace.
- Repeat checks use a respawned worker, then two checks in that worker. Process isolation is not a hostile-code filesystem/network sandbox.

- Browser validation passed: all 39 records, seven misses, 32 catches; walkthrough and probe controls; all local evidence links return HTTP 200; desktop (1440px) and mobile (390px) have no horizontal overflow; no JavaScript errors. Mobile screenshot visually reviewed.
- `git diff --check` passed.

## Review focus

- Title and opening example: does the constructed defect make the acceptance question clear?
- Benchmark section: are the misses and comparison caveats readable without overwhelming the narrative?
- Interactive explorer: all outcomes are visible and linked to downloadable evidence.

Only the article and these review notes have been prepared. No commit, push, Pages configuration change, or homepage update has been performed. The canonical URL is provisional until publication.

## Editorial revision

Rewritten around the reviewer’s responsibility, the ambition of a useful second opinion, concrete reuse opportunities, and the value of a small reproducer. Technical evidence is available in a collapsible supporting section. The previous interactive benchmark explorer was removed to keep the article focused on its human purpose.

## Publication approval

Author approved the visual redesign and GitHub publication on November 13, 2026. The final page uses a hero diagram, duration slider, selectable 39-record map, and visual comparison cards. Desktop/mobile interaction checks passed.

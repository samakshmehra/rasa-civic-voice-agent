# Evaluation scenarios

Seven scenarios for the engine's simulation framework. An LLM plays the caller
from each `simulation_context`; a second LLM judges the finished transcript
against `goals.criteria`, and deterministic assertions check tracker events.

## Running

The runner is the Rasa MCP server, driven from a coding agent:

```bash
rasa tools run --mode stdio      # loads .env
```

then ask it to run `eval/scenarios/`. Needs `RASA_LICENSE` and
`OPENAI_API_KEY`, and `rest` plus `inspector` enabled in `integrations.yml`
(they are). Results land in `eval/results/<timestamp>/`.

Every run bills tokens twice — once for the caller, once for the judge — and
makes live OpenStreetMap calls, which are rate-limited to about one per second.

## What each one is for

| Scenario | The behaviour it protects |
| --- | --- |
| `location_unmappable_still_files` | A caller who can name no landmark still leaves with a complaint number, filed at locality level in their own words |
| `location_mapped_landmark` | A known landmark is pinned, described as pinned, and the caller picks the map result |
| `duplicate_attached_not_refiled` | "Same one" attaches a report to the existing complaint instead of filing a second |
| `complaint_completes_without_early_exit` | The call is not treated as finished before anything is filed |
| `out_of_scope_offers_ward_office` | A seventh problem type is declined plainly, not bent into one of the six |
| `track_resolved_disputed` | A resolution note is read as written, and a dispute reaches a human |
| `faq_answers_from_config` | Target times come from configuration, never from a plausible guess |

## Run the flaky one more than once

`complaint_completes_without_early_exit` covers an intermittent failure — the
model occasionally ends the skill part way through with nothing filed
(finding #19). One pass proves almost nothing about it. Run it ten times and
count; that number is the finding, and it does not exist yet.

The same caution applies to reading any single result here. These scenarios
were written from failures observed by hand, which means they encode what has
already gone wrong — not what has not gone wrong yet.

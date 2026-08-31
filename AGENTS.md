# Civico — notes for coding agents

This project is a Rasa **Skills** agent (`calm_v2`), pinned to
`rasa-pro==3.19.0.dev3`. It is **not** CALM v1 — never add `config.yml`,
`domain.yml`, or flow YAMLs under `data/`.

## Layout

Skills live under `skills/<name>/` as `skill.md` with optional `memory.yml`,
`responses.yml`, `references/`. Shared tools are in `tools/civic.py` and must be
listed in a skill's `import_tools`. The mock backend is `lib/database.py` over SQLite seeded
from `data/source/*.json`. Locations are geocoded live via `lib/geocode.py`
(OpenStreetMap Nominatim, no API key).

## Control levers

Before editing a skill, understand: `tool_constraints`, `requires`,
`requires_confirmation`, `on_success`, `on_failure`, `if:` markers,
`:::ordered_block id=x`, `utter:`, `@skill.<name>`, `@block.<id>`.

Conditions use fully namespaced memory — `session.report_complaint.category`,
`session.project.area`.

## House rules for this project

- **Routing and target times are fictional demo data.**
  `data/source/demo_routing.json`, read by `route_to_authority`, contains the
  six mock category routes. OpenStreetMap supplies place facts; the demo file
  supplies departments and target days. Never present those as official.
- **Memory fields written by tools must not declare a `description`** — that is a
  hard validation error. Descriptions belong only on `llm_settable` fields.
- Complaint numbers are always spoken character by character.
- Locations are landmarks, never postal addresses. Location runs in two stages:
  `resolve_broad_location` / `confirm_broad_location` fix the locality and PIN,
  then `locate_incident` finds the spot inside it. Both return candidates on
  purpose — the citizen picks. Never auto-pick the first match.
- `locate_incident` owns the whole decision: it searches, counts attempts, and
  settles at locality level when the map cannot help. The model supplies the
  caller's words and a cleaned query, nothing more. Do not move that judgement
  back into the prompt — every live failure came from doing so.
- Tools must not resolve paths from `__file__`; use `project_root()`. The model
  archive omits `data/`, so `__file__` points into a temp dir at runtime.
- No authentication. You should not have to log in to report a pothole.

## Workflow

    make verify     # always run first
    make train
    make inspect        # text + voice
    make demo-failure   # submit_complaint forced to fail, exercises on_failure

Log anything surprising in `FINDINGS.md` — that file is the point of this project.

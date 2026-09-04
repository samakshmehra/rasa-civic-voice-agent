# Civico — a voice agent for a municipal complaint line

Built on **Rasa Skills** during the Rasa Heroes beta, with **Deepgram** for
speech-to-text and text-to-speech and **OpenStreetMap** for location.

Someone rings up, says a streetlight has been out for a week, and hangs up with
a complaint number, a department, and the name of the ward officer who now owns
it. Or they ring to ask what happened to the one they filed last month.

> **This is a simulation.** Not a real reporting channel. Complaints live in a
> local seeded SQLite file, and departments, wards and target times are
> fictional values read from `data/source/demo_routing.json`. Location text is
> the one thing that leaves the machine — it goes to OpenStreetMap's Nominatim
> service.

Alongside the agent, [`FINDINGS.md`](FINDINGS.md) is a log of 19 things found
while building on the beta engine — four of them HIGH. That file is the real
output of this project; the agent is what produced it.

---

## Quick start

```bash
make install
make env          # writes .env from .env.example — then fill in the keys
make verify       # pre-flight: keys, project layout, skills, seed data, map
make test         # 27 deterministic tests, no network, no LLM
make train
make inspect      # talk to it — microphone or keyboard
```

`make test` proves the tools are correct. It says nothing about whether the
agent *behaves* — that is what [`eval/`](eval/) is for: seven scenarios where
one model plays the caller and another judges the transcript.

| Key | Needed for |
| --- | --- |
| `RASA_LICENSE` | Rasa Pro Developer Edition |
| `OPENAI_API_KEY` | skill routing and conversation |
| `DEEPGRAM_API_KEY` | voice only — typing works without it |

---

## What a call sounds like

```
BOT   Hello, I'm Civico. I can file a complaint, check one, or explain
      how the process works.
YOU   the streetlight outside our lane is not working
BOT   Which locality and city, or your six-digit PIN code?
YOU   Vaishali, Ghaziabad
BOT   I found two matches — Vaishali, Ghaziabad, PIN 2 0 1 0 0 1.
      Is option one right?                    ← never auto-picks
YOU   option one
BOT   Where inside Vaishali is the streetlight?
YOU   near the pole outside our lane
BOT   I couldn't match that on the map. What's the nearest shop,
      gate, building or junction?
YOU   there's nothing nearby, no shop or building at all
BOT   I've logged it to Vaishali from your description.   ← settles, does not stall
      What's wrong with the light?
...
BOT   Your complaint number is C I V one zero zero six. It's routed to
      Road Maintenance, under Demo Ward 9. The ward officer on record
      is S. Kaur, on 1800-000-0104. The service target is 7 working days.
```

---

## The interesting part: location that cannot dead-end

People do not speak addresses. They say *"near the pole outside our lane"*, and
no geocoder on earth will find that. The obvious design — keep asking until the
map matches — turns the map's ignorance into the caller's problem, and they
hang up with nothing.

So location runs in two stages, and the second stage has two acceptable endings.

**Stage one — who is responsible.** Locality and city, or a PIN code. This is
not about pinning a map marker; it decides the ward, the zone, the officer and
the department. It also produces a bounding box, which becomes the safety net
for everything after it.

**Stage two — where inside it.** The engine asks the question and stores the
answer, and storing it runs everything else:

```
      collect: spoken_location          engine asks, engine stores
                  │
        run_after_setting hook          fires on the write
                  │
       ┌──────────┴──────────┐
       │  plain cleanup       │  ~1s
       │  → search the map    │
       │         │            │
       │     found? ──yes─────┼──► choose
       │         no           │
       │  "Let me check that" │  ← said immediately
       │  model fixes the name│  ~6s
       │  → search again      │
       └──────────┬───────────┘
                  │
     ┌────────────┼────────────┐
     ▼            ▼            ▼
  choose      ask_once   settled_approximate
 map found   one question  locality + their
 → caller     → collect      own words
   picks        again
 exact                     approximate
```

Both endings file a complaint. A field officer handed *"behind the Supertech
gate, near the water tank"* and a locality has exactly what a paper complaint
always gave them, and they have always managed.

### How much precision is worth asking for

That depends on the problem, which the caller cannot be expected to know:

| Category | Needs | Questions before it settles |
| --- | --- | --- |
| `streetlight` | exact — the crew has to find one pole | 2 |
| `pothole` | exact — a road can have several | 2 |
| `drainage` | street | 1 |
| `water_supply` | street | 1 |
| `garbage` | street | 1 |
| `stray_animals` | area — animals move | 0 |

The same words get treated differently by category. *"Near the pole outside our
lane"* is accepted immediately for stray dogs, questioned once for garbage, and
questioned twice for a streetlight — then accepted anyway, because a complaint
filed at locality level beats one the caller gave up on.

These values live in `demo_routing.json`, not in a prompt.

### Who decides what

This is the design decision the project is really about.

An earlier version asked the model to classify the location first — *is this
searchable, do you need more, or is vague fine?* — then read back an action and
dispatch to one of three tools. It failed constantly, and always the same way:
announcing a verdict it had not earned, saying *"I've logged it"* without
calling anything, or asking again without calling anything.

None of those judgements ever needed a model. How precise a department needs to
be, whether the effort budget is spent, when to stop asking — all knowable from
configuration and state.

The rule that came out of it: **anything the model has to *do* rather than
*say* belongs in a tool.** A tool runs when the step runs; an instruction is a
request. So the model is asked for nothing here — the engine collects the
caller's words, and the search hangs off that write.

Correcting a mishearing still wants a model, because "Kazia bath" is Ghaziabad
and no geocoder will ever work that out. That call moved *inside* the tool,
where it cannot be skipped, and it runs only on the retry — measured, because
the accuracy needs reasoning and the reasoning costs time:

| | latency | "near Kazia bath sector four" |
| --- | --- | --- |
| minimal reasoning | 1.4s | "Kasia Bath Sector Four" |
| medium reasoning | 5.8s | **"Ghaziabad Sector 4"** |

Six seconds on every location is unacceptable and wasted whenever the caller
named somewhere the map already knows. So the first search is instant and the
model time is spent only once that has come back empty — at which point the
caller is waiting on a failure anyway. Two guards throw the correction away if
it shares no word stem with what was said, or runs long enough to be an
explanation rather than a name.

If the model materially changes a place name, it has to say so out loud before
searching. Silently "correcting" a locality routes the complaint to the wrong
ward and the caller never finds out.

---

## Honesty as a feature

Every complaint records **how** it was located, and the agent says so:

> "pinned to 100 Feet Road, Indiranagar" · "logged to Vaishali from your
> description, *near the pole outside our lane*"

The phrase is written by whichever tool settled the location, so the closing
line cannot overstate what was captured. `location_precision` is stored on the
row (`exact_landmark` or `locality_approximate`) along with the caller's own
words, so the field officer sees the same thing the caller was told.

The agent also starts out knowing nobody. There are no accounts on a civic
line, so it never assumes who is calling — it asks for a number and looks it
up, and an unrecognised number is a normal outcome, not an error.

---

## What it can do

| Skill | Capability |
| --- | --- |
| `intro` | Greet and orient |
| `report_complaint` | File a complaint — where the control levers are demonstrated |
| `track_complaint` | Status by reference, or by phone number if they've lost it |
| `civic_faq` | Process questions and target times |
| `human_handoff` | Arrange a ward-office callback |
| `goodbye` | Close the call |

Six problem categories, each routed to one fictional department with a fixed
demo target — Sanitation 2 days, Water Services 3, Street Lighting 3, Drainage
5, Animal Care 5, Road Maintenance 7. Every registered complaint also comes
back with a named ward officer, derived deterministically from the confirmed
PIN so a caller who rings twice is told the same thing. All of it is read from
config, never generated by the model, and all of it is fictional.

Duplicate detection compares confirmed coordinates against open complaints of
the same category within a radius (300 m, set by `CIVIC_DUPLICATE_RADIUS_METERS`).
If the caller says it's the same incident, a supporting report is written
against the existing complaint — it does not merely claim one was.

### Try it

```bash
make run                                    # one terminal
make demo-call                              # another — type turns yourself
make demo-call ARGS="--script vague"        # or replay a scripted call
make show-demo-data                         # what is seeded
```

`--script vague` is the one worth watching: a caller who genuinely cannot name
a landmark. `--script exact` plays one who can. `--plain` drops the colour if
you are pasting the output somewhere.

| Say this | What it exercises |
| --- | --- |
| "pothole on 100 Feet Road in Indiranagar" | duplicate detection — CIV1001 is already there |
| "streetlight out, nothing nearby I can name" | the fallback — settles approximate, still files |
| "what happened to complaint C I V one zero zero four" | `resolved` branch, reads the note verbatim |
| "status of C I V one zero zero five" | `rejected` branch, reads the reason without softening |
| "how long does a pothole take?" | deterministic answer from config |

Complaint numbers are always spoken character by character — `C I V one zero
zero one`.

---

## Progressive control

Where each lever is used, and why it earns its place:

| Control | Used in | Purpose |
| --- | --- | --- |
| `utter:` + `when:` | `report_complaint` | recording notice, exact wording, once per call |
| `:::ordered_block` | `report_complaint` | location, details and duplicate check in order |
| `requires` | every side-effecting tool | preconditions declared beside the tool |
| `requires_confirmation` | `submit_complaint`, `create_handoff_ticket` | caller approves before the side effect |
| `on_success` / `on_failure` | `submit_complaint` | exact wording on both paths |
| `if:` markers | `track_complaint` | one branch per complaint status |
| project `memory.yml` | location, contact, routing | handoff between skills |

To watch `on_failure` fire:

```bash
make demo-failure     # forces submit_complaint to fail
```

File a complaint through to the end and the agent should speak
`utter_system_down` verbatim rather than improvising an apology.

---

## Layout

| Path | What's in it |
| --- | --- |
| `agent.yml` | identity, persona, voice flags, rules |
| `integrations.yml` | LLM, plus Deepgram ASR/TTS for the Inspector |
| `endpoints.yml` | response rephraser and model groups |
| `memory.yml` | project-wide memory (`session.project.*`) |
| `skills/` | one folder per skill |
| `tools/civic.py` | the shared tools |
| `lib/refine.py` | the mishearing correction, called by the tool |
| `skills/report_complaint/tools.py` | the post-write hook that runs the search |
| `lib/authority.py` | fictional routing, wards, precision rules |
| `lib/database.py` | SQLite demo backend |
| `lib/geocode.py` | OpenStreetMap lookup, no API key |
| `data/source/` | seed data and the routing config |
| `scripts/verify_setup.py` | pre-flight diagnostics |
| `tests/test_civic.py` | 27 tests — routing, precision, location, database |
| `eval/` | 7 simulation scenarios — does it *behave* |

A Rasa Skills project: no `config.yml`, no `domain.yml`, no flow YAMLs.
Pinned to `rasa-pro==3.19.0.dev3`.

---

## Known rough edges

**An ordered-block step does not hold the model in it.** `complete_when` marks
when a step is finished; it does not stop the conversation leaving one that is
not. With the search done and its result in memory
(`location_status = choose`, `location_settled` unset), the model has read the
options aloud, said "noting the spot", and walked on into the description and
phone-number sections that follow the block. Nothing is logged when it happens.

Downstream `tool_constraints` catch it — nothing can be filed without a settled
location, so the call stalls rather than recording a complaint with no
coordinates. That is the right failure, but it is a guard written here rather
than one the block provided. Finding #21.

**The model can also end a call early**, calling `complete_skill` part way
through with nothing filed. A skill-level `complete_when` now guards it;
whether that holds in every case is not yet established, which is what
`eval/scenarios/complaint_completes_without_early_exit.yml` exists to measure.
Finding #19.

**Skill selection varies by model.** On two models tested, a broadly-described
sub-skill was activated mid-complaint, bypassing the constrained flow entirely.
That skill has been removed rather than guarded — finding #18.

**The model archive omits `data/`.** Run from the project root, or set
`CIVIC_PROJECT_ROOT`. A missing seed directory now fails loudly instead of
quietly creating an empty municipal system — finding #1.

**Project memory is write-once.** Not a rough edge of this project so much as
the rule that shaped it: a project field can be written once per session and
the second write is silently discarded. Everything that changes during a call —
flags, counters, candidate lists, the location status — lives in skill memory
for that reason. Finding #22, and the one worth reading if you are building
your own.

**OpenStreetMap coverage is uneven** and calls can fail. Lookup runs off the
async loop, the caller always picks from candidates, and an unreachable map
settles the location at locality level rather than blocking the complaint.

**Correcting a misheard place adds about ten seconds** to the turn where it
happens — a plain search, then a model call, then a second search. The tool
speaks a holding line first, but this has not been heard over the voice channel
yet, only read.

---

## Licence

[MIT](LICENSE).

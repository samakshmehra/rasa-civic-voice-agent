# Findings — Civico on the new Rasa engine

Running log for the Rasa Heroes feedback form.

Environment: `rasa-pro==3.20.0.dev6` (findings 1-20 were observed on
`3.19.0.dev3`, where the engine module was `calm_v2` rather than `mantle`;
log lines below are quoted as they were emitted). Earlier: `rasa-pro==3.19.0.dev3`, Python 3.12, macOS 15.5, OpenAI `gpt-5-mini`
(skill routing) and `gpt-5-nano` (response rephraser), Deepgram Flux/Aura,
text channel via REST. Earlier findings were observed on `gpt-5.2`.

---

## What works well

Worth saying first, because it is a lot.

- **`requires` genuinely gates.** `submit_complaint` never became callable until
  `details_verified` was true. The tool is removed from the schema, not merely
  discouraged, and that held across every run.
- **`requires_confirmation` works exactly as advertised.** The engine paused,
  spoke the confirmation response, and only dispatched on approval
  (`calm_v2.tool_confirmation.confirmed`).
- **Ordered blocks track state visibly.** The stack showed
  `report_complaint:verify_summary`, so you can always see which step you are on.
- **`utter: on: activate`** delivered the recording notice verbatim.
- **Skill-to-skill memory handoff** via `session.project.*` worked without fuss.
- **Writing a skill really is writing markdown.** Going from idea to a working
  seven-skill agent took hours, not days, and most of that was the tools.

---

## 1. `data/` is not packaged into the model — HIGH

`rasa train` copies `lib/` and `tools/` into the model archive; `rasa run`
unpacks it to a temp directory and executes the tools **from there**. `data/` is
not included.

Any tool that resolves a path relative to `__file__` therefore looks for its
files in a temp folder that does not have them. In our case the SQLite backend
silently built an empty database:

```
same function, same input:
  fresh python process  ->  ok: True, area "Indiranagar"
  inside the agent      ->  ok: false, "known_areas": []
```

**Nothing errors.** You get an empty result that reads like a bad user utterance,
not a packaging problem. We chased it as a matching bug for a while.

Note that all six official example repos use this same `Path(__file__).parent.parent`
pattern, so this is not an exotic mistake — it is the documented house style.

**Suggestion:** package `data/` too, or expose the original project root to tools
(e.g. `context.project_root`), or warn at train time when a tool reads paths.

**Workaround:** resolve the project root from the working directory instead of
`__file__` — see `project_root()` in `lib/database.py`.

## 2. A hanging tool hangs the whole conversation, forever — HIGH

We introduced a SQLite deadlock in our own tool (backing a database up onto
itself while holding a write lock — our bug, not Rasa's). But the *engine's*
behaviour around it is the finding:

- The turn never returned. No timeout, no error, no fallback utterance.
- The HTTP request hung until the client gave up.
- The server stayed wedged; the tracker endpoint stopped responding too.

In a text channel this is bad. **In a voice call it is dead air on a live phone
line**, which is the worst possible failure mode for this product.

**Suggestion:** a per-tool timeout with a configurable fallback response. Even a
crude default (30s → speak an error utterance → return control) would turn a
hung call into a recoverable one.

## 3. Ordered blocks do not hold the floor — MEDIUM

Mid-complaint, the user said *"yes that number is correct."* The engine abandoned
the running block and activated `track_complaint`. The stack went three deep:

```
report_complaint  ->  capture_location (interrupt)  ->  track_complaint
```

An ordered block is presented as the strong control lever, so an ambiguous
affirmative walking out of one is surprising. There appears to be no way to say
"this block owns the conversation until it completes."

**Suggestion:** an opt-in `exclusive: true` on a block, or at minimum document
what can and cannot interrupt one.

## 4. `@skill` is an interrupt frame, not a call — MEDIUM

The tutorial says the parent stays on the stack and resumes after the sub-skill
exports its memory. The tracker shows `frame_type: "interrupt"`. Those are
different contracts, and it explains why flow did not reliably return to the
parent. We ended up moving location capture *into* the ordered block instead,
which worked — but that costs the reusability the sub-skill existed for.

## 5. Verbatim `on: activate` fires after the LLM speaks — MEDIUM

Observed order:

```
1. "Alright, I'll start a pothole complaint for that spot."   <- LLM
2. "This call may be recorded..."                             <- verbatim notice
```

For a recording notice this is backwards: the disclosure must precede any
substantive exchange. Anyone using this lever for compliance copy — which is its
stated purpose — has the same problem.

**Suggestion:** deliver `on: activate` utterances before the first generated
turn of that skill.

## 6. Routing misses an unambiguous first utterance — MEDIUM

*"I want to report a big pothole"* and *"there is a pothole on 100 feet road in
Indiranagar"* both activated `intro` instead of `report_complaint`, on a fresh
conversation, reproducibly. The second turn then routes correctly.

The `report_complaint` description literally begins "File a new civic complaint
about a pothole…", so this is not a description-quality problem. It looks like
first-turn greeting bias.

## 7. Undocumented `system.__discovered__` — UNKNOWN

The engine extracts facts into a hidden slot with confidence scores and mappings:

```json
{"key": "pothole_size", "value": "about two feet wide", "confidence": 0.92,
 "potential_mappings": [{"skill_id": "report_complaint",
                         "entry": "description", "confidence": 0.6}]}
```

It identified the right target field and then never wrote it. This appears in no
tutorial chapter, no example repo, and no documentation we could find. Either it
is an unfinished feature or there is a way to opt in that is not written down.

## 8. `description:` on a tool-written memory field fails the build — LOW

Declaring a `description` on a field that is not `llm_settable` is a hard
validation error, not a warning. Six fields failed at once. The reasoning is
sound — a description the model never sees is dead weight — but documenting a
field for the next human reading the YAML is a natural instinct, and nothing
warns you until `rasa train`.

## 9. Undocumented `run_after_setting_<field>` hook — LOW

Every `llm_settable` field logs an INFO about a missing
`run_after_setting_<field>` hook. Eleven of them on a seven-skill project. The
hook appears in no example or tutorial, so a reader cannot tell whether they are
missing something important.

**Suggestion:** DEBUG level, or a doc link.

---

## Not the engine's fault, but worth recording

Findings about building a real voice agent that others will hit too.

- **Geocoders return the wrong place confidently.** "Bangalore City Railway
  Station" resolved to a neighbourhood in another city. Never let a geocode
  result through without a human confirming it.
- **The same landmark exists many times.** "Anand Sweets" returned three genuine
  Bengaluru locations. Auto-picking the first is a coin flip that silently routes
  a complaint to the wrong department.
- **Spoken filler breaks geocoding.** `"near 100 feet road in Indiranagar"` →
  no result; `"100 feet road Indiranagar"` → resolves. Stripping filler words
  ("near", "behind", "next to", "in front of") before the lookup fixed it, and
  made conversational English location phrases more robust.
- **OSM India coverage is uneven.** Streets, layouts, malls and pincodes resolve
  well. Individual shops are patchy — Corner House, a well-known Bengaluru
  chain, is absent. Villages are largely missing.

## 10. An unguarded "invoke this block" instruction re-triggers itself forever — HIGH

`## File it` read: *"Once `category` is set, invoke `@block.file_complaint`"* with
no `if:` guard. `category` never becomes unset, so this instruction never left
the prompt — it was telling the model to re-invoke the block on every single
turn for the rest of the call, forever.

Live reproduction: after the citizen confirmed the final summary
(`details_verified = true`), the model read this stale instruction and called
`activate({'target_id': 'report_complaint__file_complaint'})` again. That
block's first step is `execute_tool: reset_complaint_context`, which
unconditionally wiped `area`, `latitude`, `longitude`, `department`, `sla_days`
— all already correctly resolved — moments before `submit_complaint` ran.
Submission then failed with `incomplete_complaint`, which reads exactly like a
real municipal-system outage but was entirely self-inflicted.

Same root cause as the `## Close` bleed already in this log (§ "what works
well" era) — an instruction with no `if:` scope is present in the prompt at
every turn, not just the turn it was written for. Gating the instruction on
`if: not session.project.area` was the first fix, and it visibly reduced
recurrence.

**It was not sufficient by itself — see #11.**

## 11. A model can invoke a named block directly, bypassing any prose guard — HIGH

Even after gating the "invoke" instruction (#10), the model independently
called `activate({'target_id': 'report_complaint__file_complaint'})` again on a
later run — with no visible instruction telling it to. `:::ordered_block
id=...:::` appears to expose a standing, callable target for the entire life of
the skill, regardless of whether any surrounding prose currently recommends
invoking it. Gating the *instruction* only changes what the model is nudged to
do; it does not change what the model is *able* to do.

**This is the actual lesson:** a destructive first step inside a reusable
block (here, `reset_complaint_context` unconditionally clearing fields) is not
safe to write assuming the block will only ever be entered when your own prose
says so. The guarantee has to live in the tool, not the prompt.

**Fix applied:** `reset_complaint_context` no longer wipes unconditionally. It
now checks whether a location is already resolved (`area` set) and nothing has
been filed yet (`complaint_id` unset) — the signature of a spurious re-entry —
and no-ops in that case, returning `skipped: true` with a hint to continue
rather than restart. It only performs the real reset when either nothing has
been captured yet, or the previous complaint in the same call already
succeeded (`complaint_id` set), which is the one case a reset is genuinely
correct — and this incidentally restores multi-complaint-per-call support,
which the earlier prose-only gate would have permanently blocked. Covered by
3 new unit tests (`reset_complaint_context`: fresh / in-progress / completed).

**Verified live**: same reproduction, same spontaneous re-`activate`, but this
run `submit_complaint` succeeded — `CIV1006`, correct department, correct SLA,
coordinates intact throughout.

## 12. Model occasionally calls the engine's own `complete_skill` tool too early — MEDIUM

Once, immediately after setting a contact number and before `route_to_department`
or `verify_summary` had run, the model called `complete_skill` — an engine tool
never mentioned in any skill file, tutorial, or doc we've seen. The engine
caught it: it produced a rephrased safeguard response — *"We were partway
through filing your pothole complaint. Do you want to continue from where we
stopped, or should I cancel it?"* — rather than silently exiting. That recovery
behaviour is good and itself undocumented. The model reaching for early exit at
all is the concerning half.

Mitigation: added an explicit instruction — *"Do not consider this skill
finished... until submit_complaint or attach_to_existing_complaint has
succeeded."* Did not recur across the following two full live runs, but this
was observed once, so treat as reduced-likelihood, not eliminated.

## 13. `set_fields` called with no arguments — LOW

Observed once: `set_fields({})` → `{"error": "No fields provided to
set_fields."}`. Handled gracefully (model just tried a normal text reply
next), but worth noting as a minor tool-calling glitch under this engine.

## 14. Geocoding a misheard building/society name is a real, unclosed gap — INFO, not a bug

Asked the model to silently correct ASR-mangled Indian place names before
geocoding (e.g. "Kazia bath" → "Ghaziabad"). It reliably fixes well-known
city/locality names — general geography is in the model's training data. It
cannot fix an obscure building or society name it has never seen ("super tech
state" was never corrected to "Supertech Estate"). In that case the agent
correctly fell through to the graceful "that exact spot might not be mapped
yet" fallback rather than looping — the right behaviour given the constraint,
just not a full solve. A phonetic/fuzzy match on the geocoder side would close
this further; out of scope for this pass.

## 15. LLM outage: verbatim responses survive, generated turns go completely silent — HIGH

Hit accidentally when the project's OpenAI credits ran out mid-session. The
engine's behaviour split cleanly in two, and the difference is instructive:

**The good half.** The session-start greeting still reached the caller. The
rephraser failed, the engine logged
`calm_v2.orchestrator.rephrase_fallback_to_verbatim`, and delivered the
verbatim `utter_greet` text instead. Graceful degradation, exactly right —
and a real argument for putting critical wording in `responses.yml` rather
than letting the model phrase it.

**The bad half.** The very next turn, which needed a generated response, failed
outright:

```
calm_v2.turn.completed   source="llm_error"  error="LLM call failed."
calm_v2.processor        event="calm_v2.turn.failed"
```

The HTTP request returned **an empty response body**. No error utterance, no
apology, nothing for the channel to speak. In text that looks like the bot
ignored you; **on a voice call it is silence on a live line** — the same
failure shape as finding #2 (a hung tool), reached by a different route.

Note the asymmetry: an outage that the engine can answer verbatim is handled
beautifully; an outage that needs generation produces nothing at all. A
provider outage or an expired API key is one of the most likely production
failures there is, so this path deserves the same fallback treatment the
rephraser already gets.

**Suggestion:** a configurable project-level `utter_llm_unavailable`, spoken
whenever a turn dies on an LLM error, so the caller always hears *something*.

---

## 16. Control levers are boolean, so "ask at most N times, then degrade" has to live in a tool — MEDIUM

`requires`, `if:` and `complete_when` all evaluate to true or false. There is no
lever that expresses a *budget* — "let the model ask for this up to twice, then
stop requiring it". That shape is extremely common in voice: a caller who cannot
produce a mappable landmark after two tries must not be asked a third time, but
the requirement is real for the first two.

Prose alone cannot hold this. "Ask at most twice" in skill instructions is a
suggestion the model drifts off within a few turns, and it has no counter to
consult. The only reliable place was inside a tool: `assess_location` reads an
attempt counter from memory, compares it against a per-category allowance in
`demo_routing.json`, and returns the action the model must take — so the model
proposes what it heard and configuration decides whether that is good enough.

That works, and it is arguably the right home for the rule. But it means a
control that reads as conversational policy is invisible in the skill file: a
reader of `report_complaint/skill.md` cannot see that a streetlight report gets
two attempts and a stray-dog report gets none. Something like
`requires: attempts(exact_landmark) < 2`, or a `max_attempts` field on an
ordered-block step, would keep the policy where it is read.

Related: there is no engine-level notion of a step *degrading* rather than
failing. `complete_when` either passes or the step repeats, so an unmet
requirement loops. The anti-dead-end pattern — accept a lower-quality answer
rather than trap the caller — had to be built by hand out of two tools and a
counter.

## 17. `requires` on a tool constraint does not enforce an equality conjunct — HIGH

`submit_complaint` declares:

```yaml
- submit_complaint:
    requires: session.report_complaint.details_verified == True
      and session.report_complaint.duplicate_decision == "new"
      and session.project.location_settled == True
      and session.project.pincode
      ...
```

Observed directly, by writing a marker from the first line of the tool body:

```
ENTERED details_verified=True duplicate_decision=None department=Street Lighting
```

The tool body executed with `duplicate_decision` unset, while the constraint
requires it to equal `"new"`. The complaint was written to the database. Nothing
in the log distinguishes this from a satisfied constraint.

The lever is not entirely inert. In a separate conversation where the caller
asked to submit before anything had been collected, the tool body was never
entered and no `calm_v2.tool.dispatched` line appeared at all — so `requires`
does block in some states. The difference between the two cases is what the
unmet conjuncts were: all-empty memory blocked, whereas a single unmet
`== "new"` comparison against `None` did not.

Two consequences worth separating:

1. A constraint that reads as a hard precondition silently is not one. Anything
   guarded only by an equality conjunct in `requires` needs the same check
   duplicated inside the tool body.
2. Because it holds in the obvious test case — ask to submit with nothing
   filled in — it is easy to conclude the lever works and stop testing there.

In this project the duplicated in-tool guard is what actually prevented a bad
write earlier in the same session: with `details_verified` unset, the tool body
was entered and returned `incomplete_complaint`, so `on_failure` spoke
`utter_system_down` and no row was created. The engine-level lever did not stop
that call; the hand-written one did.

## 18. Skill selection is strongly model-dependent, and a loosely-described skill is a trap — HIGH

The same project, the same eleven-turn call, three models:

| Model | Outcome |
| --- | --- |
| `gpt-5-mini` | completed — filed CIV1006 at `locality_approximate` with the caller's words |
| `gpt-5.6-terra` | never filed — reached the end and said "I'll submit the confirmed complaint now", then did not call the tool |
| `gpt-5.6-luna` | never filed — left the complaint entirely and raised a ward-office callback ticket instead |

The most damaging failure was shared by both 5.6 models and never once seen on
`gpt-5-mini`: mid-complaint, they activated the `capture_location` sub-skill
instead of `report_complaint`'s own location step. That skill exists for
looking up *past* complaints by neighbourhood, and its `resolve_landmark` has
no confirmed PIN code to scope to. Both models sent "near the pole outside our
lane" into it, got back **Electric Pole, Patamata Lanka, Vijayawada** — roughly
two thousand kilometres from the confirmed Ghaziabad locality — and confirmed
it to the caller as the incident location.

Nothing in the engine prevented this. `report_complaint` does not import
`resolve_landmark`; the model reached it by activating a different skill whose
`description` read "Find out where a problem or an existing complaint is". That
sentence is the entire attack surface. Skill descriptions are matched
semantically, so a broadly-worded one silently competes with the constrained
flow it was never meant to replace, and the constrained flow's `requires`,
PIN-scoping and precision rules are all bypassed at once.

Two things follow for anyone building on this:

1. A skill's `description` is a routing decision, not documentation. Ours now
   says explicitly what it is *not* for.
2. Description wording alone is not a control. The reliable fix was a guard
   inside the tool itself — `resolve_landmark` now refuses outright while a
   complaint is in progress and names the tools that should have been used.

Worth flagging for the beta: there appears to be no way to declare that a skill
is unavailable while another skill is active, or to scope a sub-skill to the
callers that are meant to reach it. `tool_constraints` guard tools; nothing
guards the skill boundary.

## 19. Engine-provided tools cannot be constrained, so a skill can be abandoned mid-flow — HIGH

`complete_skill`, `cancel_skill`, `activate` and `set_fields` are supplied by
the engine. They are callable at every turn and there is no lever that governs
them.

Observed repeatedly: partway through a complaint — category set, locality and
PIN confirmed, description and callback number captured — the model calls
`complete_skill` and ends with "is there anything else?". Nothing is filed.

It consistently survives the first half. Category, the two-stage location, and
the fallback to a locality-level location all complete reliably. The derail
happens after the description step: the model either re-asks for a locality it
has already confirmed, or ends the skill outright. Both leave `location_settled`
true and the complaint unfiled, so state is intact — it is the flow position
that is lost, not the data. It
is intermittent, not rare; it happened on both attempts at recording a demo
call, having not appeared in several runs before that. The skill file says, in
plain prose:

> Do not consider this skill finished until either `@tool.submit_complaint` or
> `@tool.attach_to_existing_complaint` succeeds.

That instruction is advisory, and on this turn it lost.

The obvious fix does not work. Declaring a constraint fails validation:

```yaml
- complete_skill:
    requires: session.project.complaint_id
```
```
[calm_v2.validation.tools.unresolved_tool] Skill 'report_complaint' references
tool 'complete_skill', but no matching @tool function was found
```

`tool_constraints` resolves only user-defined `@tool` functions, so the tools
most capable of destroying a flow are the only ones that cannot be guarded.
Everything a developer writes can be gated by `requires`; the engine's own
control verbs cannot.

This matters more than it first appears. The whole progressive-control pitch is
that preconditions live in configuration rather than in prompt text. That holds
right up to the moment the model decides the conversation is over — and at that
point the only available control is a sentence in the prompt, which is exactly
what the levers exist to replace.

What would help, roughly in order of usefulness:

1. `tool_constraints` accepting engine tools, so `complete_skill` can require
   the same state that `submit_complaint` requires.
2. A skill-level `complete_when`, mirroring the one on ordered-block steps.
3. At minimum, a logged warning when `complete_skill` fires while an ordered
   block still has unfinished steps — the run above produced no such signal.

## 20. `set_` is a reserved tool-name prefix, and the resulting error points somewhere else — LOW

Naming a tool `set_problem_category` fails the build with:

```
[calm_v2.validation.tools.unresolved_tool] Skill 'report_complaint' references
tool 'set_problem_category', but no matching @tool function was found in
skills/report_complaint/tools.py, skills/report_complaint/tools/, or the
declared shared tools/ folder.
```

The function is there, decorated with `@tool`, in the declared shared folder.
The real cause is a reserved prefix — presumably guarding the engine's own
`set_fields` — and it is reported separately, at WARNING level, in a line that
does not say what the rule is:

```
[calm_v2.tool_loader.shared_tool.reserved_prefix] tool_name=set_problem_category
```

Two small things would save the next person the detour: have the validation
error name the actual cause rather than telling you to add a function that
already exists, and have the warning say which prefixes are reserved.

Renaming to `record_problem_category` fixed it immediately.

## 21. On 3.20, an ordered-block step cannot rely on the model calling a tool — HIGH

The migration itself was straightforward. Three breaking changes, each with an
error message that named its own cause:

1. `rasa.calm_v2.*` is now `rasa.mantle.*`.
2. The inline `llm:` block in `integrations.yml` is gone — `llm:` takes a
   `model_group:` reference and the provider details move onto the group.
3. Project memory fields may no longer declare `llm_settable: true`.

Under an hour, and `verify`, 26 tests and `train` all pass on the new pin.

What did not survive is the behaviour. With an unchanged skill file and an
unchanged ordered block, the step that asks the model to call `locate_incident`
stopped producing the call. The model answers the caller in prose instead:

```
caller │ near Galaxy Picture Hall
Civico │ Got it. Is that the exact spot, or should I file it as the lane
       │ near Galaxy Picture Hall?
caller │ there is nothing else nearby, just the pole
Civico │ Understood. I've got the location, and I'll file it there.
```

Nothing filed, `location_settled` unset, no error anywhere. The tool's
`requires` was satisfied and the `:::ordered_block` syntax matches the shipped
3.20 examples.

**The diagnosis is the useful part.** Running `examples/mantle-voice-agent`
from this repository on the same pin, its ordered block works — and its steps
are shaped differently from ours. Every instruction step there completes by the
model *setting a memory field*; the only tool call in the block is an
`execute_tool` step, which the engine runs itself. Not one step depends on the
model choosing to call a custom tool part-way through.

So the reliable primitives inside an ordered block appear to be:

| Step shape | Driven by | Reliable here |
| --- | --- | --- |
| `execute_tool:` | engine | yes |
| `instructions:` completing on a field write | model, via `set_fields` | yes |
| `instructions:` expecting a custom tool call | model's own choice | **no** |

That third row is not documented as weaker than the others, and nothing warns
you when a step ends without the call its instructions asked for.

### The workaround, and where it stands

`run_after_setting_<entry>` is the right mechanism: an engine-invoked post-write
hook that fires when a settable field is written, never offered to the LLM,
taking no arguments. Restructured so the model writes `spoken_location` and
`cleaned_query`, and the hook resolves the location.

Two things worth recording for anyone else trying this:

- A shared hook in `tools/` cannot target a skill-local field:
  `[mantle.validation.tools.invalid_post_write_hook] must target a settable
  skill memory entry`. It has to live in the owning skill's `tools.py`.
- The hook does fire, and its rollback works: returning a non-null `error`
  removed a `spoken_location` written before any area was confirmed, which is
  exactly right — a location recorded with nowhere to put it makes the step
  look answered.

What is still not solved is getting the model to write the field at the moment
the step wants it. It writes `category` reliably through the same mechanism, so
the difference is something about this step rather than field writes in
general. Unresolved, and the reason this finding is filed as HIGH: the
recommended way to build a strict sequence has a step shape that quietly does
nothing.

## Still to test

- [x] Forced submit failure returns `ok: False` and writes no complaint (automated test)
- [ ] Engine `on_failure` delivers `utter_system_down` in the Inspector — **zero uses across all six official examples**
- [ ] Voice end to end through the Inspector with Deepgram
- [ ] A ~1s network geocode inside a live voice turn — does the agent go silent?
- [ ] Complaint number read back over TTS (`C I V one zero zero one`)
- [ ] Mid-block category change: "it's not a pothole, it's an open manhole"
- [ ] Emergency rule (fire/accident → 112) breaking out of an active block
- [ ] Conversational-English spoken-input test through Deepgram Flux, not typed
- [x] Location fallback ladder end to end — confirmed live on `gpt-5-mini`.
      Two failed landmark searches (`exact_attempts` reached the streetlight
      allowance of 2), then `accept_approximate_location`, then a filed
      complaint: CIV1006, `location_precision = locality_approximate`, with
      the caller's own words persisted as `location_note`.
- [ ] Whether `requires` enforces equality conjuncts in any state (see #17)
- [ ] Why `gpt-5.6-terra` announces a submit it never performs (see #18) —
      state was complete except `duplicate_decision`, which it never set
- [ ] Whether the ordered block can hold step position at all on 5.6-class
      models, or whether it only ever held on `gpt-5-mini`
- [x] Full happy path end to end on `gpt-5.4-mini` — CIV1006 filed at
      `exact_landmark`, correct ward officer, duplicate detection caught the
      seeded complaint nearby, location read back honestly.
- [x] Approximate path end to end — settles at `locality_approximate` with
      the caller's own words once the effort budget is spent.
- [ ] How often `complete_skill` ends a complaint early (see #19). Frequent
      enough to block a demo recording, and no rate measured yet. It tends to
      fire just after the callback number, before the duplicate check — worth
      checking whether the ordered block loses its position there.

---

## Application hardening completed

- Duplicate attachment now writes an idempotent `complaint_reports` record.
- Duplicate detection now uses confirmed coordinates and a configurable radius,
  rather than treating an entire neighbourhood as one location.
- Complaint references are allocated inside an atomic SQLite transaction.
- Spoken complaint references such as "C I V one zero zero four" are normalized
  deterministically before lookup.
- Location choice no longer defaults to option one, and malformed candidate
  memory returns a controlled tool error.
- The FAQ reads fictional departments and demo target days through
  `route_to_department` instead of letting the model invent them.
- Ward-office callback tickets are now persisted instead of generated and lost.
- Deterministic demo-routing, map-adapter, and database coverage lives in
  `tests/test_civic.py`.
- The problem category is recorded by a tool, not an `llm_settable` field. As
  a field, the model reliably announced it had noted the category — "I'll
  register the streetlight complaint" — without writing anything, and the whole
  flow then ran with no category: location started before the locality was
  confirmed, and the caller was asked what kind of problem it was after
  describing it twice. A tool call either happens or it does not.
- Every path in `assess_location` that asks the caller a question now spends
  the attempt budget. Two did not: the `needs_more` branch, and the override
  branch where the model says a spot is good enough and configuration
  disagrees. Only failed map searches were counted, so a model that re-asked
  without ever searching looped forever — observed live on `gpt-5.6-terra`,
  which asked for the same landmark eight turns running.
- The unscoped single-stage location path is gone. `capture_location`,
  `resolve_landmark` and `confirm_location` were removed outright rather than
  guarded: complaint lookup now works from the caller's phone number, which
  needs no map and cannot resolve to the wrong city (see #18).
- Locating an incident is one tool call, not three. `assess_location`,
  `resolve_exact_landmark` and `accept_approximate_location` were collapsed
  into `locate_incident`, which searches, counts and settles internally. The
  model no longer classifies the location or dispatches between tools —
  every observed live failure came from it doing one of those two things.
- The caller's description accumulates instead of being overwritten. A late
  "there is nothing nearby" was replacing "near the pole outside our lane",
  handing the field officer the refusal instead of the description.
- A failed map search can no longer block filing. Each category declares how
  precisely it needs locating and how many attempts to spend; once spent, the
  complaint files at locality level with the caller's own words, recorded as
  `location_precision = locality_approximate` and read back honestly.

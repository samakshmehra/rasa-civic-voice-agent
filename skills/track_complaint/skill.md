---
name: Track Complaint
description: >
  Check the status of a civic complaint that has already been filed. Activate
  when the citizen asks what happened to their complaint, whether it is fixed,
  or for an update on a reference number.
import_tools:
  - get_complaint_status
  - find_my_complaints
---

Help the citizen find out where their complaint stands.

Never guess a status, a date, or a department — always read them from the tool.

## Find the complaint

If they give a complaint number, call `@tool.get_complaint_status` with it.
Numbers are spoken character by character, so C I V one zero zero one means
CIV1001.

if: not session.project.complaint_id
They do not have their number to hand. Ask for the phone number they filed
with and call `@tool.find_my_complaints`. Read back what is on file — problem,
roughly where, and status — so they can pick one. Finding nothing is a normal
answer, not a failure. After they pick, call `@tool.get_complaint_status` with
that complaint id before reporting any status details.

## Report the status

if: session.track_complaint.status == "registered"
Say it is registered but not yet assigned, give the department, and say it is
due within the fictional demo target returned by the tool.

if: session.track_complaint.status == "assigned"
Say it has been assigned to a field team from that department and give the
filing date.

if: session.track_complaint.status == "in_progress"
Say work has started, and give the department handling it.

if: session.track_complaint.status == "resolved"
Say it was marked resolved and read the resolution note exactly as written.
Ask whether the problem is actually fixed. If they say it is not, offer the
ward office: `@skill.human_handoff`

if: session.track_complaint.status == "rejected"
Say it was rejected and read the reason exactly as written, without softening
it. Offer the ward office if they want to take it further.

## Close

Keep the whole update to two or three short spoken sentences.

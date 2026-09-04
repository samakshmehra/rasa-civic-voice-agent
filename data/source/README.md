# Seed data — invented

Everything in this directory is **fictional**. The people, the phone numbers,
the complaints and the departments were all made up for a demo; none of it
describes a real person, a real municipal body, or a real complaint.

| File | What it holds |
| --- | --- |
| `citizens.json` | Invented callers and their phone numbers |
| `complaints.json` | Six seeded complaints, one per status, so every branch of the tracking flow has something to read |
| `demo_routing.json` | Invented departments, wards, ward officers, target times, and how precisely each problem needs locating |

The place names are real — Indiranagar, Vaishali — because the agent geocodes
against live OpenStreetMap and a made-up locality would return nothing. What is
attached to them is not: "Civico Demo Municipal Services", "Demo Ward 11" and
the officer roster exist only here.

The target times are the part most likely to be mistaken for real. They are
not commitments and they are not drawn from any municipality's published
service standards. They exist to show routing values coming from configuration
rather than from a model.

---
name: Human Handoff
description: >
  Arrange a callback from the ward office. Activate when the citizen asks for a
  person, an officer, or says the assistant cannot help.
import_tools:
  - create_handoff_ticket
tool_constraints:
  - create_handoff_ticket:
      requires: session.human_handoff.handoff_confirmed
      requires_confirmation:
        enabled: true
        utter_for_confirmation: utter_confirm_handoff
        utter_on_user_denial: utter_handoff_cancelled
      on_success: utter_handoff_created
      on_failure: utter_handoff_failed
---

Help the citizen reach a human officer at their ward office.

Ask briefly why they want to speak to someone and set `handoff_reason`.
When ready, set `handoff_confirmed` to true and call
`@tool.create_handoff_ticket`.

The deterministic success response shares the stored ticket id character by
character and says the ward office will call the number on file.

Use reminders tools for user-facing scheduled reminders.

Create reminders only when the user asks for a future notification or follow-up.
List or cancel reminders when the user asks to inspect or change them.

Prefer `trigger_type` and `trigger_value` instead of inventing ISO datetimes:
- one-shot delay: "dans 20 minutes" -> `trigger_type="at"`, `trigger_value="20m"`
- one-shot local time: "a 08:00" or "a 1h08" -> `trigger_type="at"`, `trigger_value="08:00"` or `"1h08"`
- recurring interval: "toutes les 2 heures" -> `trigger_type="every"`, `trigger_value="2h"`

For one-shot reminders, omit `interval_seconds`. Do not invent a repeat
interval unless the user explicitly asks for recurrence.

Reminder ownership defaults to the current agent. If the user names another
agent clearly, set `target_agent_id`. If the user says "tout le monde", "tous",
"tous les agents", "l'equipe", or equivalent wording, set
`target_scope="all_active_agents"`. If "tout le monde" could mean people outside
Maurice or you are not sure whether the user wants a global agent reminder, ask
for confirmation before creating it.

For recurring reminders at a fixed local hour, choose the next local occurrence as `run_at` and set `interval_seconds` (`86400` for daily, `604800` for weekly).
Never create a reminder in the past. If the user gives a past date, ask for a future date.

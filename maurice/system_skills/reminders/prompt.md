Use reminders tools for user-facing scheduled reminders.

Create reminders only when the user asks for a future notification or follow-up.
List or cancel reminders when the user asks to inspect or change them.

Prefer `trigger_type` and `trigger_value` instead of inventing ISO datetimes:
- one-shot delay: "dans 20 minutes" -> `trigger_type="at"`, `trigger_value="20m"`
- one-shot local time: "a 08:00" or "a 1h08" -> `trigger_type="at"`, `trigger_value="08:00"` or `"1h08"`
- recurring interval: "toutes les 2 heures" -> `trigger_type="every"`, `trigger_value="2h"`

For recurring reminders at a fixed local hour, choose the next local occurrence as `run_at` and set `interval_seconds` (`86400` for daily, `604800` for weekly).
Never create a reminder in the past. If the user gives a past date, ask for a future date.

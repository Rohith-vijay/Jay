---
name: weekly-review-planning
description: "Weekly reset: what was finished, what is slipping, what is waiting, and a realistic plan for next week."
---

_Adapted for Jay from the Hermes Agent skill of the same name (NousResearch, MIT licence, see LICENSE-Hermes.txt)._

A bounded review once a week (Sunday evening works well). Recommend first, change things only with the user's yes.

## Procedure
1. **Window.** Last 7 days behind, next 14 ahead. State it.
2. **Evidence.** `list_tasks` (done, overdue, upcoming), `calendar_feed` if connected, `get_work_patterns` for when
   the user actually works, exam and deadline notes from `search_knowledge`, and unread deadline mail via `mail_list`.
3. **Wins.** What got done. Say it plainly; it matters for motivation.
4. **Slipping.** Overdue or repeatedly rescheduled tasks: for each, either a smaller next step, a new date, or "drop it".
5. **Waiting.** Things others owe the user (replies, results, approvals) and promises the user made. Suggest
   follow-ups with a date. Silence is not completion.
6. **Plan.** Count the real free hours from the calendar and past work patterns. Pick at most 3 outcomes for the
   week and the near-term next actions. Put hard deadlines and exams first, then placement preparation, then the rest.
   Name what is being deferred. Never fill every hour.
7. **Apply** only what the user approves: `add_task`, `update_task`, reminders, and a recurring weekly routine via
   `add_routine` if they want this every week.

## Output
Wins / Overdue or at risk / Waiting and follow-ups / Next week's three outcomes / Deferred / Proposed changes.

---
name: assignment-and-exam-tracker
description: "Collect assignment deadlines and exam dates from the ERP, LMS and mail, and turn them into scheduled work."
---

Goal: nothing academic is missed, and the dates live in Jay's schedule, not only on a portal.

## Procedure
1. **Sources.** `open_portals` (erp, lms, outlook) to see which are signed in. If one needs a sign-in, ask the user
   to sign in themselves in Jay's browser window and continue after they say done. Never type their password.
2. **ERP.** Read the timetable, attendance, internal marks and notices with `browser_read`, `browser_links` and
   `browser_click`. Exam dates and fee deadlines first.
3. **LMS.** Read each course's assignments and quizzes with their due dates. Check any calendar feed (`calendar_feed`).
4. **Mail.** `mail_list` with subject words like exam, assignment, deadline, submission, hall ticket, last date, and
   `mail_read` the matches.
5. **Merge and de-duplicate.** One line per item: course, what, due date and time, where you found it. If sources
   disagree, show both.
6. **Schedule.** `list_tasks` first so nothing is added twice. Then `add_task` for each deadline, plus a start date
   with working time for big items (see the `plan` routine), and a reminder the day before for exams.
7. **Keep it fresh.** Offer `add_watch` on the notices page (kind site) and on college mail (kind email), so changes
   reach the user's phone.

## Output
Exams / Due this week / Due later / Needs the user (sign-in, missing info) / Added to schedule.

# Cron recipes — reminders & automations via hermes's built-in cronjob

Reminders and recurring automations are **not** implemented as MCP tools. hermes
already ships a `cronjob` toolset (`create` / `list` / `update` / `pause` /
`resume` / `run` / `remove`) that schedules jobs and runs each one in a fresh
agent session. We reuse it.

The only new primitive secretary-mcp provides is delivery: **`line_send_message`**
(and `line_send_media` / `line_send_file`). A scheduled job runs in a fresh
session with no chat context, so its prompt must be **self-contained**: it must
name the target LINE id explicitly and call `line_send_message` to deliver.

> Exact parameter names/syntax for creating a cron job are in the hermes cronjob
> tool reference. Below we give the **schedule** and the **job prompt** — the
> prompt is the part that matters and is what you paste into the job.

---

## Reminder (one-shot)

User says: *"提醒我 30 分鐘後回電給王經理"*. The agent creates a cronjob that fires
once at that time with this prompt:

```
Call line_send_message with:
  to = "U196d1445f7fe156eac44c02106f364ec"
  text = "提醒：回電給王經理"
Then remove this cron job (it is a one-shot reminder).
```

Notes:
- If the hermes cronjob only supports recurring cron expressions, make the job
  self-deleting by ending the prompt with "then remove this cron job" (as above),
  so a one-shot reminder does not repeat.
- The reminder text is fixed at creation time — no tools other than
  `line_send_message` are needed.

---

## Daily morning summary

Schedule: every day at 08:00 (cron `0 8 * * *`, timezone Asia/Taipei). Job prompt:

```
You are generating a morning summary for LINE user U196d1445f7fe156eac44c02106f364ec.
1. Call todo_list to get pending tasks.
2. Call the calendar tool (google-calendar MCP) to list today's events.
3. Compose a short summary in Traditional Chinese with emojis, e.g.:
   ☀️ 早安！今日摘要
   📅 行程：...
   📝 待辦：...
4. Deliver it by calling line_send_message with
   to = "U196d1445f7fe156eac44c02106f364ec" and text = <the summary>.
If there is nothing to report, still send a brief good-morning note.
```

---

## Pre-meeting reminder

Schedule: every 15 minutes (cron `*/15 * * * *`). Job prompt:

```
For LINE user U196d1445f7fe156eac44c02106f364ec:
1. Call the calendar tool to list today's events.
2. For any event starting within the next 15 minutes that has not been notified,
   call line_send_message with
     to = "U196d1445f7fe156eac44c02106f364ec"
     text = "📅 15分鐘後有會議：{title}（{time}）"
3. If there are no upcoming events, do nothing (send no message).
```

Note: "not been notified yet" has no shared state across fresh sessions. If you
need strict de-duplication, have the job also record notified event ids via a
todo/marker, or accept occasional duplicates. Simplest robust option: schedule
per-event one-shot reminders (like the reminder recipe) at event-creation time
instead of polling.

---

## Weekly report

Schedule: every Friday at 17:00 (cron `0 17 * * 5`). Job prompt:

```
Generate a weekly report for LINE user U196d1445f7fe156eac44c02106f364ec:
1. Call the calendar tool for this week's events.
2. Call todo_list for completed vs pending tasks.
3. Call expense_report for this month's spending.
4. Call attendance_report for this month's attendance.
5. Compose a Traditional Chinese report:
   📊 本週工作報告
   📅 會議/行程：X 場
   📝 待辦完成：X / Y
   💰 花費：NT$X
   🕐 出勤：X 天
6. Deliver via line_send_message with to = "U196d1445f7fe156eac44c02106f364ec".
```

---

## Managing jobs

Use the hermes cronjob tool directly:
- `list` — see all scheduled jobs.
- `pause` / `resume` — temporarily disable/enable.
- `remove` — delete (also how a user cancels a reminder/automation).
- `run` — fire once now, for testing a job prompt without waiting.

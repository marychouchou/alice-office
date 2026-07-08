// reminder — dedicated, single-purpose scheduling tools backed by hermes's
// built-in cron. These exist because a small model reliably calls a simple
// two-argument tool but is unreliable at driving the general-purpose `cronjob`
// tool from a casual request. Each tool here does ONE thing with a tiny schema;
// the heavy lifting (time parsing, cron wiring) happens in code, not the model.
//
// Design: reminder_set parses the time, then shells out to `hermes cron create`
// to register a ONE-SHOT job whose prompt explicitly calls line_send_message —
// the same recipe that already fires reliably (an explicit cron prompt is easy
// for the model to follow). Delivery therefore still flows through the built-in
// cron + line_send_message path; nothing here talks to LINE directly.
import { z } from "zod";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const execFileP = promisify(execFile);

// Taipei is a fixed UTC+8 (no DST); the MCP host may run in any timezone, so we
// never rely on the process's local time for absolute clock times.
const TZ_OFFSET_MS = 8 * 60 * 60 * 1000;

/** Resolve the hermes CLI: env override, then ~/.local/bin, then PATH. */
function hermesBin() {
  if (process.env.HERMES_BIN) return process.env.HERMES_BIN;
  const local = path.join(os.homedir(), ".local", "bin", "hermes");
  return fs.existsSync(local) ? local : "hermes";
}

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/** Current wall-clock in Taipei, as {y,mo,d} (mo is 1-based). */
function taipeiToday() {
  const t = new Date(Date.now() + TZ_OFFSET_MS);
  return { y: t.getUTCFullYear(), mo: t.getUTCMonth() + 1, d: t.getUTCDate() };
}

/** Epoch ms for a Taipei wall-clock date/time. */
function taipeiEpoch(y, mo, d, h, mi) {
  return Date.UTC(y, mo - 1, d, h, mi, 0, 0) - TZ_OFFSET_MS;
}

/**
 * Parse a natural-language time into a target epoch (ms). Relative forms are
 * timezone-independent; bare clock times are interpreted in Asia/Taipei.
 * Returns null if nothing matches.
 * @param {string} raw
 * @returns {number|null}
 */
function parseWhen(raw) {
  if (!raw) return null;
  const s = raw.trim();
  const now = Date.now();

  // ISO 8601 with explicit zone/offset — trust it as-is.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) {
    const t = Date.parse(s);
    return Number.isNaN(t) ? null : t;
  }

  const lower = s.toLowerCase();

  // Relative: "in 5 minutes" / "in 2 hours"
  const enRel = lower.match(/^in\s+(\d+)\s*(min(?:ute)?s?|hours?|hrs?|h|m)\b/);
  if (enRel) {
    const n = parseInt(enRel[1], 10);
    const isHour = /^h|hour|hr/.test(enRel[2]);
    return now + n * (isHour ? 3600000 : 60000);
  }

  // Relative (Chinese): "10分鐘後" / "2小時後" / "30分後"
  const zhRel = s.match(/(\d+)\s*(分鐘|分|小時|鐘頭)(?:鐘)?\s*後?/);
  if (zhRel && /後|later|$/.test(s)) {
    const n = parseInt(zhRel[1], 10);
    const isHour = zhRel[2] === "小時" || zhRel[2] === "鐘頭";
    return now + n * (isHour ? 3600000 : 60000);
  }

  // Absolute date-time: "2026-07-01 14:30" (Taipei)
  const dateTime = s.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$/);
  if (dateTime) {
    const [, y, mo, d, h, mi] = dateTime;
    return taipeiEpoch(+y, +mo, +d, +h, +mi);
  }

  // "tomorrow 9:00" / "明天9:00" / "明天 9:00"
  const tomorrow = lower.match(/^(?:tomorrow|明天|明日)\s*(?:at\s+)?(\d{1,2}):(\d{2})$/);
  if (tomorrow) {
    const { y, mo, d } = taipeiToday();
    return taipeiEpoch(y, mo, d, +tomorrow[1], +tomorrow[2]) + 86400000;
  }

  // Bare / today clock time: "today 14:00" / "今天14:00" / "14:00"
  const clock = lower.match(/^(?:today\s+(?:at\s+)?|今天\s*)?(\d{1,2}):(\d{2})$/);
  if (clock) {
    const { y, mo, d } = taipeiToday();
    let t = taipeiEpoch(y, mo, d, +clock[1], +clock[2]);
    if (t <= now) t += 86400000; // already passed today → tomorrow
    return t;
  }

  return null;
}

/** Format an epoch as a friendly Taipei string, e.g. "7/1 14:30". */
function friendlyTaipei(epoch) {
  const t = new Date(epoch + TZ_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, "0");
  return `${t.getUTCMonth() + 1}/${t.getUTCDate()} ${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}`;
}

// ---------------------------------------------------------------------------

/**
 * Register the reminder tools on the MCP server.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 */
export function registerReminderTools(server) {
  server.tool(
    "reminder_set",
    "Set a one-off reminder that is delivered to the user on LINE at a given time. " +
    "Use this whenever the user asks to be reminded of something (e.g. \"10分鐘後提醒我去寄信\", " +
    "\"提醒我明天9點開會\"). Do NOT use the calendar for reminders.",
    {
      when: z.string().describe(
        "When to remind, natural language: relative like '10分鐘後' / 'in 5 minutes' / '2小時後', " +
        "or a clock time like '14:30' / '明天9:00' / '2026-07-01 14:30' (clock times are Asia/Taipei)."),
      text: z.string().describe("What to remind the user about (the reminder content)."),
    },
    (args) => setReminder(args),
  );

  server.tool(
    "reminder_list",
    "List the reminders currently scheduled (one-off reminder jobs).",
    {},
    () => listReminders(),
  );

  server.tool(
    "reminder_cancel",
    "Cancel a scheduled reminder by its job id (get the id from reminder_list).",
    { id: z.string().describe("The reminder/cron job id to cancel.") },
    (args) => cancelReminder(args),
  );
}

async function setReminder({ when, text }) {
  if (!text) return ok({ error: "text is required (what to remind about)." });
  if (!when) return ok({ error: "when is required (e.g. '10分鐘後', 'in 5 minutes', '明天9:00')." });

  const target = parseWhen(when);
  if (target == null) {
    return ok({ error: `無法解析時間 "${when}"。可用：'10分鐘後'、'in 5 minutes'、'14:30'、'明天9:00'、'2026-07-01 14:30'。` });
  }
  const now = Date.now();
  if (target <= now) return ok({ error: "提醒時間必須在未來。" });

  const delayMin = Math.max(1, Math.ceil((target - now) / 60000));
  const name = `reminder-${now.toString(36)}`;
  // Explicit, imperative prompt — the cron session's model follows this reliably.
  const prompt =
    `Call the line_send_message tool now with text="⏰ 提醒：${text}" ` +
    `(do not set the \`to\` argument; it defaults to the configured user). ` +
    `This is a one-shot reminder — after the message is sent, remove this cron job.`;

  try {
    const { stdout } = await execFileP(
      hermesBin(),
      ["cron", "create", `${delayMin}m`, prompt, "--name", name, "--deliver", "local"],
      { env: process.env, timeout: 60000, maxBuffer: 1024 * 1024 },
    );
    const jobId = (stdout.match(/Created job:\s*([0-9a-f]+)/i) || [])[1] || null;
    return ok({
      success: true,
      jobId,
      name,
      remindAt: friendlyTaipei(target),
      inMinutes: delayMin,
      text,
    });
  } catch (e) {
    return ok({ error: `建立提醒失敗：${e.stderr || e.message}` });
  }
}

async function listReminders() {
  try {
    const { stdout } = await execFileP(hermesBin(), ["cron", "list"], { env: process.env, timeout: 30000, maxBuffer: 1024 * 1024 });
    return ok({ success: true, output: stdout.trim() });
  } catch (e) {
    return ok({ error: `列出提醒失敗：${e.stderr || e.message}` });
  }
}

async function cancelReminder({ id }) {
  if (!id) return ok({ error: "id is required (get it from reminder_list)." });
  try {
    const { stdout } = await execFileP(hermesBin(), ["cron", "remove", id], { env: process.env, timeout: 30000, maxBuffer: 1024 * 1024 });
    return ok({ success: true, output: stdout.trim() });
  } catch (e) {
    return ok({ error: `取消提醒失敗：${e.stderr || e.message}` });
  }
}

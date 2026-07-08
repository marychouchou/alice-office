// attendance — daily clock-in/clock-out tracking, backed by a JSON file.
import { z } from "zod";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * @typedef {Object} AttendanceRecord
 * @property {string}      date               YYYY-MM-DD (Asia/Taipei)
 * @property {string|null} clockIn            ISO datetime of clock-in, or null
 * @property {string|null} clockOut           ISO datetime of clock-out, or null
 * @property {string|null} clockInLocation    optional location label at clock-in
 * @property {string|null} clockOutLocation   optional location label at clock-out
 * @property {number|null} workMinutes        total minutes worked, filled on clock-out
 * @property {number|null} overtime           minutes over 480 (8 hours), filled on clock-out
 * @property {string|null} note
 */

// One JSON file holds every user's records: { [lineUserId]: AttendanceRecord[] }.
const STORE = path.join(os.homedir(), ".hermes", "secretary-attendance.json");

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/** Return today's date string in YYYY-MM-DD (Asia/Taipei). */
const today = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Taipei" });

/**
 * Return the current datetime as a local string in Asia/Taipei formatted as
 * "YYYY-MM-DDTHH:MM:SS" (no offset). Used for clock-in/out timestamps.
 */
const nowTaipei = () =>
  new Date().toLocaleString("sv-SE", { timeZone: "Asia/Taipei" }).replace(" ", "T");

/** Read the whole store. Returns {} if the file does not exist yet. */
function readStore() {
  try {
    return JSON.parse(fs.readFileSync(STORE, "utf-8"));
  } catch {
    return {};
  }
}

/** Persist the whole store, creating ~/.hermes if needed. */
function writeStore(store) {
  fs.mkdirSync(path.dirname(STORE), { recursive: true });
  fs.writeFileSync(STORE, JSON.stringify(store, null, 2));
}

/** Read one user's records. @param {string} key storage key (line user id). */
function getRecords(key) {
  return readStore()[key] ?? [];
}

/** Replace one user's records and write back. */
function saveRecords(key, records) {
  const store = readStore();
  store[key] = records;
  writeStore(store);
}

/**
 * Find today's record for a user (if any).
 * @param {string} key
 * @returns {{ records: AttendanceRecord[], record: AttendanceRecord|undefined, idx: number }}
 */
function findTodayRecord(key) {
  const records = getRecords(key);
  const d = today();
  const idx = records.findIndex((r) => r.date === d);
  return { records, record: idx >= 0 ? records[idx] : undefined, idx };
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/**
 * Register all attendance_* tools on the MCP server.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 * @param {string} lineUserId  single-tenant id from SECRETARY_LINE_USER_ID
 */
export function registerAttendanceTools(server, lineUserId) {
  const key = lineUserId || "default";

  server.tool(
    "attendance_in",
    "Record clock-in for today.",
    { location: z.string().optional().describe("Optional location label (e.g. office, home)") },
    (args) => clockIn(key, args),
  );

  server.tool(
    "attendance_out",
    "Record clock-out for today and compute work hours.",
    { location: z.string().optional().describe("Optional location label") },
    (args) => clockOut(key, args),
  );

  server.tool(
    "attendance_status",
    "Return today's clock-in / clock-out status.",
    {},
    () => status(key),
  );

  server.tool(
    "attendance_report",
    "Return a work-hours report for a given month.",
    { month: z.string().optional().describe("YYYY-MM; defaults to the current month") },
    (args) => report(key, args),
  );
}

/**
 * Record clock-in for today. Rejects if already clocked in.
 * @param {string} key
 * @param {{ location?: string }} args
 */
function clockIn(key, { location }) {
  const { records, record: todayRecord, idx } = findTodayRecord(key);
  const now = nowTaipei();

  if (todayRecord?.clockIn) {
    return ok({ error: "Already clocked in today.", clockIn: todayRecord.clockIn });
  }

  /** @type {AttendanceRecord} */
  const record = {
    date: today(),
    clockIn: now,
    clockOut: null,
    clockInLocation: location || null,
    clockOutLocation: null,
    workMinutes: null,
    overtime: null,
    note: null,
  };

  if (idx >= 0) records[idx] = { ...records[idx], ...record };
  else records.push(record);
  saveRecords(key, records);

  return ok({ success: true, action: "clock_in", clockIn: now, location: location || null });
}

/**
 * Record clock-out for today and compute work/overtime minutes.
 * Overtime = minutes worked beyond 8 hours (480 min).
 * @param {string} key
 * @param {{ location?: string }} args
 */
function clockOut(key, { location }) {
  const { records, record: todayRecord, idx } = findTodayRecord(key);
  const now = nowTaipei();

  if (!todayRecord?.clockIn) {
    return ok({ error: "Not clocked in today. Clock in first." });
  }
  if (todayRecord.clockOut) {
    return ok({ error: "Already clocked out today.", clockOut: todayRecord.clockOut });
  }

  const workMinutes = Math.round(
    (new Date(now).getTime() - new Date(todayRecord.clockIn).getTime()) / 60000,
  );
  const overtime = Math.max(0, workMinutes - 480);

  records[idx] = { ...todayRecord, clockOut: now, clockOutLocation: location || null, workMinutes, overtime };
  saveRecords(key, records);

  return ok({
    success: true,
    action: "clock_out",
    clockOut: now,
    location: location || null,
    workMinutes,
    overtime,
  });
}

/**
 * Return today's attendance status.
 * @param {string} key
 */
function status(key) {
  const { record } = findTodayRecord(key);
  if (!record) return ok({ date: today(), status: "not_clocked_in" });
  return ok({
    date: today(),
    status: record.clockOut ? "clocked_out" : "working",
    clockIn: record.clockIn,
    clockOut: record.clockOut,
    workMinutes: record.workMinutes,
    clockInLocation: record.clockInLocation,
  });
}

/**
 * Summarise work hours for the target month (defaults to current month).
 * @param {string} key
 * @param {{ month?: string }} args
 */
function report(key, { month }) {
  const records = getRecords(key);
  const targetMonth = month || today().slice(0, 7); // YYYY-MM
  const monthly = records.filter((r) => r.date.startsWith(targetMonth));

  const totalMinutes = monthly.reduce((s, r) => s + (r.workMinutes ?? 0), 0);
  const totalOvertime = monthly.reduce((s, r) => s + (r.overtime ?? 0), 0);
  const avgMinutes = monthly.length > 0 ? Math.round(totalMinutes / monthly.length) : 0;

  return ok({
    month: targetMonth,
    totalDays: monthly.length,
    totalMinutes,
    avgMinutesPerDay: avgMinutes,
    overtimeMinutes: totalOvertime,
    records: monthly.map((r) => ({
      date: r.date,
      clockIn: r.clockIn,
      clockOut: r.clockOut,
      workMinutes: r.workMinutes,
      overtime: r.overtime,
    })),
  });
}

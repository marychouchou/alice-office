// expense — expense tracking with per-user records, backed by a JSON file.
import { z } from "zod";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * @typedef {Object} ExpenseRecord
 * @property {string}      id           short random id
 * @property {number}      amount       amount in TWD
 * @property {string}      currency     always "TWD" for now
 * @property {string}      category     one of CATEGORIES
 * @property {string}      description  human-readable description
 * @property {string}      date         YYYY-MM-DD
 * @property {string}      createdAt    ISO timestamp
 * @property {string|null} receiptPath  local file path to receipt, or null
 * @property {boolean}     reimbursed   whether the expense has been reimbursed
 */

/** Allowed expense categories. */
const CATEGORIES = ["meals", "transport", "supplies", "entertainment", "travel", "communication", "other"];

// One JSON file holds every user's records: { [lineUserId]: ExpenseRecord[] }.
const STORE = path.join(os.homedir(), ".hermes", "secretary-expenses.json");

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/** Return today's date in YYYY-MM-DD (Asia/Taipei). */
const today = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Taipei" });

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

/** Read one user's expense records. @param {string} key storage key. */
function getRecords(key) {
  return readStore()[key] ?? [];
}

/** Replace one user's records and write back. */
function saveRecords(key, records) {
  const store = readStore();
  store[key] = records;
  writeStore(store);
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/**
 * Register all expense_* tools on the MCP server.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 * @param {string} lineUserId  single-tenant id from SECRETARY_LINE_USER_ID
 */
export function registerExpenseTools(server, lineUserId) {
  const key = lineUserId || "default";

  server.tool(
    "expense_add",
    "Record a new expense.",
    {
      amount: z.number().positive().describe("Amount in TWD"),
      description: z.string().describe("What the expense was for"),
      category: z.enum(["meals", "transport", "supplies", "entertainment", "travel", "communication", "other"])
        .optional()
        .describe("Expense category; defaults to 'other'"),
      date: z.string().optional().describe("YYYY-MM-DD; defaults to today"),
      receiptPath: z.string().optional().describe("Local file path to receipt image/PDF"),
    },
    (args) => addExpense(key, args),
  );

  server.tool(
    "expense_list",
    "List expenses for a month with a category breakdown.",
    { month: z.string().optional().describe("YYYY-MM; defaults to current month") },
    (args) => listExpenses(key, args),
  );

  server.tool(
    "expense_report",
    "Return a summary report of expenses for a month.",
    { month: z.string().optional().describe("YYYY-MM; defaults to current month") },
    (args) => reportExpenses(key, args),
  );

  server.tool(
    "expense_reimburse",
    "Mark an expense as reimbursed.",
    { expenseId: z.string().describe("Expense id") },
    (args) => reimburseExpense(key, args),
  );

  server.tool(
    "expense_remove",
    "Delete an expense record.",
    { expenseId: z.string().describe("Expense id") },
    (args) => removeExpense(key, args),
  );
}

/**
 * Create a new expense record.
 * @param {string} key
 * @param {{ amount: number, description: string, category?: string, date?: string, receiptPath?: string }} args
 */
function addExpense(key, { amount, description, category, date, receiptPath }) {
  if (!amount || amount <= 0) return ok({ error: "Amount must be positive." });
  if (!description) return ok({ error: "Description is required." });

  const records = getRecords(key);
  const cat = CATEGORIES.includes(category) ? category : "other";

  /** @type {ExpenseRecord} */
  const record = {
    id: crypto.randomUUID().slice(0, 8),
    amount,
    currency: "TWD",
    category: cat,
    description,
    date: date || today(),
    createdAt: new Date().toISOString(),
    receiptPath: receiptPath || null,
    reimbursed: false,
  };

  records.push(record);
  saveRecords(key, records);

  const monthTotal = records
    .filter((r) => r.date.startsWith(record.date.slice(0, 7)))
    .reduce((s, r) => s + r.amount, 0);

  return ok({ added: true, expense: record, monthTotal });
}

/**
 * List expenses for the target month, newest first, with a category breakdown.
 * @param {string} key
 * @param {{ month?: string }} args
 */
function listExpenses(key, { month }) {
  const records = getRecords(key);
  const targetMonth = month || today().slice(0, 7);
  const filtered = records
    .filter((r) => r.date.startsWith(targetMonth))
    .sort((a, b) => b.date.localeCompare(a.date));

  const byCategory = {};
  for (const r of filtered) {
    byCategory[r.category] = (byCategory[r.category] || 0) + r.amount;
  }

  const total = filtered.reduce((s, r) => s + r.amount, 0);
  const unreimbursed = filtered.filter((r) => !r.reimbursed).reduce((s, r) => s + r.amount, 0);

  return ok({ month: targetMonth, count: filtered.length, total, unreimbursed, byCategory, expenses: filtered });
}

/**
 * Summarise expenses for the target month broken down by category.
 * @param {string} key
 * @param {{ month?: string }} args
 */
function reportExpenses(key, { month }) {
  const records = getRecords(key);
  const targetMonth = month || today().slice(0, 7);
  const filtered = records.filter((r) => r.date.startsWith(targetMonth));

  const byCategory = {};
  for (const r of filtered) {
    if (!byCategory[r.category]) byCategory[r.category] = { count: 0, total: 0 };
    byCategory[r.category].count++;
    byCategory[r.category].total += r.amount;
  }

  const total = filtered.reduce((s, r) => s + r.amount, 0);
  const reimbursed = filtered.filter((r) => r.reimbursed).reduce((s, r) => s + r.amount, 0);

  return ok({
    month: targetMonth,
    count: filtered.length,
    total,
    reimbursed,
    pending: total - reimbursed,
    byCategory,
  });
}

/**
 * Mark an expense as reimbursed by id.
 * @param {string} key
 * @param {{ expenseId: string }} args
 */
function reimburseExpense(key, { expenseId }) {
  if (!expenseId) return ok({ error: "expenseId is required." });
  const records = getRecords(key);
  const item = records.find((r) => r.id === expenseId);
  if (!item) return ok({ error: `Expense not found: ${expenseId}` });
  item.reimbursed = true;
  saveRecords(key, records);
  return ok({ reimbursed: true, expense: item });
}

/**
 * Delete an expense record by id.
 * @param {string} key
 * @param {{ expenseId: string }} args
 */
function removeExpense(key, { expenseId }) {
  if (!expenseId) return ok({ error: "expenseId is required." });
  const records = getRecords(key);
  const idx = records.findIndex((r) => r.id === expenseId);
  if (idx === -1) return ok({ error: `Expense not found: ${expenseId}` });
  const removed = records.splice(idx, 1)[0];
  saveRecords(key, records);
  return ok({ removed: true, expense: removed });
}

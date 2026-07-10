// todo — a persistent per-user task list, backed by a JSON file.
import { z } from "zod";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * @typedef {Object} TodoItem
 * @property {string}      id          short random id
 * @property {string}      text        task description
 * @property {boolean}     done        completion flag
 * @property {"high"|"medium"|"low"} priority
 * @property {string|null} dueDate     ISO date / natural language, or null
 * @property {string}      createdAt   ISO timestamp
 * @property {string|null} completedAt ISO timestamp when completed, else null
 */

// One JSON file holds every user's list: { [lineUserId]: TodoItem[] }.
const STORE = path.join(os.homedir(), ".hermes", "secretary-todos.json");

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/** Read the whole store. Returns {} if the file does not exist yet. */
function readStore() {
  try {
    return JSON.parse(fs.readFileSync(STORE, "utf-8"));
  } catch {
    return {};
  }
}

/** Persist the whole store, creating the ~/.hermes directory if needed. */
function writeStore(store) {
  fs.mkdirSync(path.dirname(STORE), { recursive: true });
  fs.writeFileSync(STORE, JSON.stringify(store, null, 2));
}

/** Read one user's list. @param {string} key  storage key (line user id). */
function getTodos(key) {
  return readStore()[key] ?? [];
}

/** Replace one user's list and write back. */
function saveTodos(key, todos) {
  const store = readStore();
  store[key] = todos;
  writeStore(store);
}

/**
 * Register all todo_* tools on the MCP server. Read this first for an overview
 * of the available tools and their arguments; the handlers below hold the logic.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 * @param {string} lineUserId  single-tenant id from SECRETARY_LINE_USER_ID
 */
export function registerTodoTools(server, lineUserId) {
  const key = lineUserId || "default";

  server.tool(
    "todo_add",
    "Add a task to the to-do list.",
    {
      text: z.string().describe("Task description"),
      priority: z.enum(["high", "medium", "low"]).optional().describe("Defaults to medium"),
      dueDate: z.string().optional().describe("Due date (ISO 8601 or natural language)"),
    },
    (args) => addTodo(key, args),
  );

  server.tool(
    "todo_list",
    "List tasks, highest priority first. Pending-only unless showDone is true.",
    {
      showDone: z.boolean().optional().describe("Include completed tasks"),
    },
    (args) => listTodos(key, args),
  );

  server.tool(
    "todo_complete",
    "Mark a task as done.",
    { todoId: z.string().describe("Task id") },
    (args) => completeTodo(key, args),
  );

  server.tool(
    "todo_remove",
    "Delete a task.",
    { todoId: z.string().describe("Task id") },
    (args) => removeTodo(key, args),
  );
}

// ---------------------------------------------------------------------------
// Handlers — pulled out of the tool registration so each reads on its own.
// Every handler takes the storage key plus the validated tool arguments.
// ---------------------------------------------------------------------------

/**
 * Create a new todo and return it together with the remaining pending count.
 * @param {string} key
 * @param {{ text: string, priority?: string, dueDate?: string }} args
 */
function addTodo(key, { text, priority, dueDate }) {
  if (!text) return ok({ error: "Todo text is required." });
  const todos = getTodos(key);
  /** @type {TodoItem} */
  const item = {
    id: crypto.randomUUID().slice(0, 8),
    text,
    done: false,
    priority: priority || "medium",
    dueDate: dueDate || null,
    createdAt: new Date().toISOString(),
    completedAt: null,
  };
  todos.push(item);
  saveTodos(key, todos);
  return ok({ added: true, todo: item, totalPending: todos.filter((t) => !t.done).length });
}

/**
 * List todos, highest priority first. Pending-only unless showDone is true.
 * @param {string} key
 * @param {{ showDone?: boolean }} args
 */
function listTodos(key, { showDone }) {
  const todos = getTodos(key);
  const filtered = showDone ? todos : todos.filter((t) => !t.done);
  const rank = { high: 0, medium: 1, low: 2 };
  const sorted = [...filtered].sort((a, b) => rank[a.priority] - rank[b.priority]);
  return ok({ count: sorted.length, totalDone: todos.filter((t) => t.done).length, todos: sorted });
}

/**
 * Mark a todo done by id.
 * @param {string} key
 * @param {{ todoId: string }} args
 */
function completeTodo(key, { todoId }) {
  if (!todoId) return ok({ error: "todoId is required." });
  const todos = getTodos(key);
  const item = todos.find((t) => t.id === todoId);
  if (!item) return ok({ error: `Todo not found: ${todoId}` });
  item.done = true;
  item.completedAt = new Date().toISOString();
  saveTodos(key, todos);
  return ok({ completed: true, todo: item });
}

/**
 * Delete a todo by id.
 * @param {string} key
 * @param {{ todoId: string }} args
 */
function removeTodo(key, { todoId }) {
  if (!todoId) return ok({ error: "todoId is required." });
  const todos = getTodos(key);
  const idx = todos.findIndex((t) => t.id === todoId);
  if (idx === -1) return ok({ error: `Todo not found: ${todoId}` });
  const removed = todos.splice(idx, 1)[0];
  saveTodos(key, todos);
  return ok({ removed: true, todo: removed });
}

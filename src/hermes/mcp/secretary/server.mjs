#!/usr/bin/env node
// secretary-mcp — an MCP server exposing secretary tools (todo, meeting,
// translate) to an agent over stdio.
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { registerTodoTools } from "./tools/todo.mjs";
import { registerMeetingTools } from "./tools/meeting.mjs";
import { registerTranslateTools } from "./tools/translate.mjs";
import { registerAttendanceTools } from "./tools/attendance.mjs";
import { registerExpenseTools } from "./tools/expense.mjs";
import { registerMapsTools } from "./tools/maps.mjs";
import { registerLineTools } from "./tools/line.mjs";
import { registerReminderTools } from "./tools/reminder.mjs";

// Load secrets from a .env file sitting next to this script (not cwd — the
// Hermes gateway spawns this process with an unpredictable cwd). Values
// already present in process.env (e.g. injected by a host-mode VM install
// via ~/.profile) take precedence; loadEnvFile never overwrites them.
//
// Any failure here is swallowed, not just missing-file (ENOENT): the router
// always bind-mounts this path when HOST_SECRETARY_MCP_DIR is set (it can't
// reliably check host-side existence from inside its own container — see
// container_manager.py's _build_volume_config), so if the file was actually
// absent on the host, Docker may have auto-vivified an empty directory here
// instead (read fails with EISDIR, not ENOENT). Either way every secret
// below already has an env-var fallback (empty string), so failing to load
// this file must never crash the whole MCP server.
try {
  process.loadEnvFile(join(dirname(fileURLToPath(import.meta.url)), ".env"));
} catch (err) {
  console.error(`[secretary-mcp] no .env loaded (${err.code ?? err.message}); using process env only`);
}

// Single-tenant server: SECRETARY_LINE_USER_ID identifies the user that owns
// per-user state (todos, attendance, expenses) and is the default file_send target.
const LINE_USER_ID = process.env.SECRETARY_LINE_USER_ID || "";

// Google Maps API key for maps_search and maps_details.
// Leave unset to disable maps tools (they will return a configuration error).
const MAPS_API_KEY = process.env.GOOGLE_MAPS_API_KEY || "";

// LINE Messaging API channel access token, used by the line_* send tools.
// Leave unset to disable them (they return a configuration error).
const LINE_CHANNEL_ACCESS_TOKEN = process.env.SECRETARY_LINE_CHANNEL_ACCESS_TOKEN || "";

// Public base URL of the file-host service that serves staged media/documents,
// e.g. https://files.example.com. Required by line_send_media / line_send_file.
const FILE_HOST_BASE_URL = process.env.SECRETARY_FILE_HOST_BASE_URL || "";

// LINE ids on the wire keep their upper-case prefix ("U"/"C"/"R" + hex), but
// SECRETARY_LINE_USER_ID doubles as a per-user state key whose first letter is
// stored lower-cased elsewhere in hermes. Restore the prefix for the line_*
// tools' default target only — the storage-key usages above (todo/attendance/
// expense) must keep the lower-cased form.
const LINE_DEFAULT_TARGET = LINE_USER_ID
  ? LINE_USER_ID.charAt(0).toUpperCase() + LINE_USER_ID.slice(1)
  : "";

const server = new McpServer({ name: "secretary", version: "0.1.0" });

registerTodoTools(server, LINE_USER_ID);
registerMeetingTools(server);
registerTranslateTools(server);
registerAttendanceTools(server, LINE_USER_ID);
registerExpenseTools(server, LINE_USER_ID);
registerMapsTools(server, MAPS_API_KEY);
registerLineTools(server, {
  channelToken: LINE_CHANNEL_ACCESS_TOKEN,
  defaultTarget: LINE_DEFAULT_TARGET,
  fileHostBaseUrl: FILE_HOST_BASE_URL,
});
registerReminderTools(server);

const transport = new StdioServerTransport();
await server.connect(transport);
// stdout is reserved for the MCP protocol; all logging goes to stderr.
console.error(`[secretary-mcp] ready; lineUserId=${LINE_USER_ID || "(unset->default)"}`);

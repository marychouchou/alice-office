// line — LINE Messaging API send primitives (text, media, document link).
// Everything that touches the LINE push wire format lives here; renaming the
// platform later means changing only this file.
//
// Files are served by a separate, long-lived file-host service (see
// ../../file-host/), reachable at SECRETARY_FILE_HOST_BASE_URL. This tool only
// stages a copy of the file into the shared cache dir and builds the public
// URL — it does not run any HTTP server itself.
//
// LINE push wire-format facts this file relies on:
//   - There is NO generic "file" message type. Documents (PDF/Word/...) cannot
//     be sent inline; they must be delivered as a text message containing a link
//     (line_send_file). Only image/video/audio can be sent as media.
//   - image  : originalContentUrl (JPEG/PNG), previewImageUrl (JPEG/PNG)
//   - video  : originalContentUrl (mp4), previewImageUrl (JPEG/PNG, required)
//   - audio  : originalContentUrl (m4a), duration in milliseconds (required)
//   - Max 5 message objects per push; URLs must be HTTPS.
import { z } from "zod";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

const LINE_PUSH_API = "https://api.line.me/v2/bot/message/push";
// Shared with the file-host service; the tool writes here, the host serves it.
// Must match file_host.py — both honour FILE_CACHE_DIR and otherwise default to
// the XDG cache dir (~/.cache/secretary-mcp/file-cache). Not tied to ~/.hermes.
const CACHE_DIR = process.env.FILE_CACHE_DIR
  ? path.resolve(process.env.FILE_CACHE_DIR)
  : path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "secretary-mcp", "file-cache");

const IMAGE_EXT = new Set([".jpg", ".jpeg", ".png"]);
const VIDEO_EXT = new Set([".mp4"]);
const AUDIO_EXT = new Set([".m4a"]);

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/**
 * POST a single message object to the LINE push API.
 * @param {string} channelToken
 * @param {string} to  LINE user/group/room id
 * @param {Record<string, unknown>} message  a LINE message object
 * @returns {Promise<{ ok: boolean, status: number, body: string }>}
 */
async function linePush(channelToken, to, message) {
  console.error('[linePush]', JSON.stringify({ to, messages: [message] }));
  const res = await fetch(LINE_PUSH_API, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${channelToken}` },
    body: JSON.stringify({ to, messages: [message] }),
  });
  return { ok: res.ok, status: res.status, body: res.ok ? "" : await res.text() };
}

/**
 * Copy a local file into the shared cache under a random token and return the
 * public URL served by the file-host. The host applies a TTL to staged files.
 * @param {string} baseUrl   SECRETARY_FILE_HOST_BASE_URL (trailing slash ok)
 * @param {string} filePath  absolute local path (must exist)
 * @returns {{ token: string, name: string, url: string }}
 */
function stageFile(baseUrl, filePath) {
  const name = path.basename(filePath);
  const token = crypto.randomBytes(16).toString("hex");
  const dir = path.join(CACHE_DIR, token);
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(filePath, path.join(dir, name));
  const url = `${baseUrl.replace(/\/+$/, "")}/files/${token}/${encodeURIComponent(name)}`;
  return { token, name, url };
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/**
 * Register the LINE send tools on the MCP server.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 * @param {{ channelToken: string, defaultTarget: string, fileHostBaseUrl: string }} cfg
 *        channelToken     — LINE Messaging API channel access token
 *        defaultTarget    — LINE id used when `to` is omitted
 *        fileHostBaseUrl  — public base URL of the file-host (for media/files)
 */
export function registerLineTools(server, cfg) {
  server.tool(
    "line_send_message",
    "Send a plain text message to a LINE user or group. Use this to deliver reminders, " +
    "summaries, notifications, or a link for the user to open.",
    {
      text: z.string().describe("Message text to send"),
      to: z.string().optional().describe("LINE user or group id; defaults to the configured user"),
    },
    (args) => sendMessage(cfg, args),
  );

  server.tool(
    "line_send_media",
    "Send an image, video, or audio file to LINE as inline media (the file appears in the chat). " +
    "Only .jpg/.jpeg/.png (image), .mp4 (video), or .m4a (audio) are supported — LINE cannot send " +
    "documents inline; use line_send_file for PDFs and other documents.",
    {
      filePath: z.string().describe("Absolute path to a local .jpg/.png/.mp4/.m4a file"),
      to: z.string().optional().describe("LINE user or group id; defaults to the configured user"),
      previewImagePath: z.string().optional()
        .describe("Absolute path to a JPEG/PNG preview image — required for video"),
      durationMs: z.number().int().optional()
        .describe("Audio length in milliseconds (required by LINE for audio; defaults to 60000)"),
    },
    (args) => sendMedia(cfg, args),
  );

  server.tool(
    "line_send_file",
    "Send any local file (PDF, Word, Excel, etc.) to LINE as a downloadable link. " +
    "The file is hosted temporarily and delivered as a text message containing the URL. " +
    "Use this for documents; use line_send_media for images/video/audio.",
    {
      filePath: z.string().describe("Absolute path to the local file to send"),
      to: z.string().optional().describe("LINE user or group id; defaults to the configured user"),
      note: z.string().optional().describe("Optional text shown above the download link"),
    },
    (args) => sendFile(cfg, args),
  );
}

/**
 * Send a plain text message.
 * @param {{ channelToken: string, defaultTarget: string }} cfg
 * @param {{ text: string, to?: string }} args
 */
async function sendMessage({ channelToken, defaultTarget }, { text, to }) {
  const target = to || defaultTarget;
  if (!text) return ok({ error: "text is required." });
  if (!target) return ok({ error: "No target: pass `to`, or set SECRETARY_LINE_USER_ID." });
  if (!channelToken) return ok({ error: "SECRETARY_LINE_CHANNEL_ACCESS_TOKEN is not configured." });

  const res = await linePush(channelToken, target, { type: "text", text });
  if (!res.ok) return ok({ error: `LINE push failed: ${res.status} ${res.body}` });
  return ok({ success: true, sentTo: target, type: "text" });
}

/**
 * Send an image / video / audio file as inline LINE media, served via the file-host.
 * @param {{ channelToken: string, defaultTarget: string, fileHostBaseUrl: string }} cfg
 * @param {{ filePath: string, to?: string, previewImagePath?: string, durationMs?: number }} args
 */
async function sendMedia({ channelToken, defaultTarget, fileHostBaseUrl }, { filePath, to, previewImagePath, durationMs }) {
  const target = to || defaultTarget;
  if (!filePath) return ok({ error: "filePath is required." });
  if (!target) return ok({ error: "No target: pass `to`, or set SECRETARY_LINE_USER_ID." });
  if (!channelToken) return ok({ error: "SECRETARY_LINE_CHANNEL_ACCESS_TOKEN is not configured." });
  if (!fileHostBaseUrl) return ok({ error: "SECRETARY_FILE_HOST_BASE_URL is not configured." });

  const resolved = path.resolve(filePath);
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
    return ok({ error: `File not found: ${filePath}` });
  }

  const ext = path.extname(resolved).toLowerCase();
  const staged = stageFile(fileHostBaseUrl, resolved);

  let message;
  if (IMAGE_EXT.has(ext)) {
    message = { type: "image", originalContentUrl: staged.url, previewImageUrl: staged.url };
  } else if (AUDIO_EXT.has(ext)) {
    message = { type: "audio", originalContentUrl: staged.url, duration: durationMs ?? 60000 };
  } else if (VIDEO_EXT.has(ext)) {
    if (!previewImagePath) {
      return ok({ error: "video requires previewImagePath (a JPEG/PNG preview image)." });
    }
    const previewResolved = path.resolve(previewImagePath);
    if (!fs.existsSync(previewResolved)) return ok({ error: `Preview image not found: ${previewImagePath}` });
    const previewStaged = stageFile(fileHostBaseUrl, previewResolved);
    message = { type: "video", originalContentUrl: staged.url, previewImageUrl: previewStaged.url };
  } else {
    return ok({
      error: `Unsupported media type '${ext}'. Use .jpg/.png (image), .mp4 (video), or .m4a (audio); ` +
        `for documents use line_send_file.`,
    });
  }

  const res = await linePush(channelToken, target, message);
  if (!res.ok) return ok({ error: `LINE push failed: ${res.status} ${res.body}`, url: staged.url });
  return ok({ success: true, sentTo: target, type: message.type, url: staged.url });
}

/**
 * Send any file as a downloadable link (text message). Used for documents that
 * LINE cannot deliver as inline media.
 * @param {{ channelToken: string, defaultTarget: string, fileHostBaseUrl: string }} cfg
 * @param {{ filePath: string, to?: string, note?: string }} args
 */
async function sendFile({ channelToken, defaultTarget, fileHostBaseUrl }, { filePath, to, note }) {
  const target = to || defaultTarget;
  if (!filePath) return ok({ error: "filePath is required." });
  if (!target) return ok({ error: "No target: pass `to`, or set SECRETARY_LINE_USER_ID." });
  if (!channelToken) return ok({ error: "SECRETARY_LINE_CHANNEL_ACCESS_TOKEN is not configured." });
  if (!fileHostBaseUrl) return ok({ error: "SECRETARY_FILE_HOST_BASE_URL is not configured." });

  const resolved = path.resolve(filePath);
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
    return ok({ error: `File not found: ${filePath}` });
  }

  const staged = stageFile(fileHostBaseUrl, resolved);
  const text = note ? `${note}\n${staged.url}` : `${staged.name}\n${staged.url}`;

  const res = await linePush(channelToken, target, { type: "text", text });
  if (!res.ok) return ok({ error: `LINE push failed: ${res.status} ${res.body}`, url: staged.url });
  return ok({ success: true, sentTo: target, type: "file-link", fileName: staged.name, url: staged.url });
}

// meeting — stateless tools for audio transcription and minutes. The actual
// transcription and summarising are done by the agent's own speech/language
// models; these tools only validate input and return structured instructions
// for the agent to act on.
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/**
 * Register the meeting_* tools on the MCP server. Read this first for an
 * overview; the handlers below hold the logic.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 */
export function registerMeetingTools(server) {
  server.tool(
    "meeting_transcribe",
    "Return audio metadata and transcription guidance (the agent's speech pipeline does the actual transcription).",
    { audioPath: z.string().describe("Local path to the audio file") },
    (args) => transcribe(args),
  );

  server.tool(
    "meeting_summarize",
    "Turn a transcript into structured meeting minutes (produced by the agent's language model).",
    { transcript: z.string().describe("Meeting transcript text") },
    (args) => summarize(args),
  );
}

/**
 * Validate an audio file and return its metadata plus transcription guidance.
 * Does not transcribe itself — the agent's media pipeline (or an existing
 * transcript already in the conversation) produces the text.
 * @param {{ audioPath: string }} args
 */
function transcribe({ audioPath }) {
  if (!audioPath) {
    return ok({
      error: "audioPath is required; the user should send an audio message via LINE.",
      hint: "LINE auto-downloads inbound audio; the local path is usually in the message context (MediaPath).",
    });
  }
  if (!fs.existsSync(audioPath)) return ok({ error: `Audio file not found: ${audioPath}` });

  const stats = fs.statSync(audioPath);
  return ok({
    action: "transcribe",
    audioPath,
    fileSize: stats.size,
    extension: path.extname(audioPath).toLowerCase(),
    instructions: [
      "This audio file needs transcription. Supported formats (m4a, mp4, wav, mp3, webm, ogg)",
      "should be handled by the agent's media-understanding capability.",
      "If a transcript is already present in the conversation context, use that instead.",
      "After obtaining the transcript, call meeting_summarize to produce structured minutes.",
    ].join("\n"),
  });
}

/**
 * Return structured instructions for turning a transcript into meeting minutes.
 * The agent's own language model produces the final summary.
 * @param {{ transcript: string }} args
 */
function summarize({ transcript }) {
  if (!transcript) {
    return ok({ error: "transcript text is required; run meeting_transcribe first or paste the transcript." });
  }
  return ok({
    action: "summarize",
    transcriptLength: transcript.length,
    transcript,
    instructions: [
      "Generate structured meeting minutes from this transcript:",
      "",
      "1. Meeting summary (2-3 sentences)",
      "2. Key discussion points (bulleted)",
      "3. Decisions made",
      "4. Action items (who, what, deadline if mentioned)",
      "5. Follow-up items",
      "",
      "Keep it clear and structured. Attribute statements to speakers if identifiable.",
      "Reply in the same language as the transcript.",
    ].join("\n"),
  });
}

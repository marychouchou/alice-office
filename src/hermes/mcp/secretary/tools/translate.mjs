// translate — text translation. No external translation API: the tool returns
// instructions and the agent's own language model produces the translation.
import { z } from "zod";

/** Wrap any JSON-serialisable payload as an MCP text result. */
const ok = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }] });

/**
 * Register the translate tool on the MCP server. Read this first for an
 * overview; the handler below holds the logic.
 * @param {import("@modelcontextprotocol/sdk/server/mcp.js").McpServer} server
 */
export function registerTranslateTools(server) {
  server.tool(
    "translate",
    "Translate text (the agent's language model produces the translation).",
    {
      text: z.string().describe("Text to translate"),
      targetLanguage: z.string().optional().describe("Target language; leave empty to auto-detect"),
    },
    (args) => translate(args),
  );
}

/**
 * Build translation instructions for the agent. When no target language is
 * given, the rules fall back to "non-Chinese -> Chinese, Chinese -> English".
 * @param {{ text: string, targetLanguage?: string }} args
 */
function translate({ text, targetLanguage }) {
  if (!text) return ok({ error: "Text to translate is required." });
  const target = targetLanguage || "";
  return ok({
    action: "translate",
    sourceText: text,
    targetLanguage: target || "auto-detect",
    instructions: [
      `Translate the following text${target ? ` to ${target}` : ""}:`,
      "",
      text,
      "",
      "Rules:",
      "- If no target language is specified: translate to Chinese if the source is non-Chinese, otherwise to English.",
      "- Preserve formatting and tone.",
      "- Reply with the translation only, no explanations.",
    ].join("\n"),
  });
}

#!/usr/bin/env python3
"""
Gmail MCP Server for Hermes Agent.
Reads OAuth tokens from ~/.config/google-calendar-mcp/tokens.json
Uses GOOGLE_ACCOUNT_MODE env var to select the user account.
"""
import asyncio
import base64
import json
import os
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.path.dirname(__file__))
from token_manager import get_access_token, get_account_mode

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"

app = Server("gmail-mcp")


def gmail_request(method: str, path: str, **kwargs) -> dict:
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{GMAIL_API}{path}"
    with httpx.Client() as client:
        resp = client.request(method, url, headers=headers, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def parse_message_headers(headers: list) -> dict:
    result = {}
    for h in headers:
        name = h["name"].lower()
        if name in ("subject", "from", "to", "date", "cc"):
            result[name] = h["value"]
    return result


def get_message_body(payload: dict) -> str:
    """Recursively extract plain text body from message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if mime_type in ("multipart/alternative", "multipart/mixed", "multipart/related"):
        for part in payload.get("parts", []):
            body = get_message_body(part)
            if body:
                return body

    return ""


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="gmail_list_messages",
            description="List recent Gmail messages. Returns id, subject, from, date for each.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "Max messages to return (default 10)", "default": 10},
                    "label": {"type": "string", "description": "Label to filter (e.g. INBOX, SENT, UNREAD)", "default": "INBOX"},
                },
            },
        ),
        Tool(
            name="gmail_search_messages",
            description="Search Gmail messages using Gmail search syntax (e.g. 'from:bob subject:meeting')",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query"},
                    "max_results": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="gmail_read_message",
            description="Read the full content of a Gmail message by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="gmail_send_message",
            description="Send a new email via Gmail.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body (plain text)"},
                    "cc": {"type": "string", "description": "CC recipients (optional)"},
                },
                "required": ["to", "subject", "body"],
            },
        ),
        Tool(
            name="gmail_reply_message",
            description="Reply to an existing Gmail thread/message.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Message ID to reply to"},
                    "body": {"type": "string", "description": "Reply body (plain text)"},
                },
                "required": ["message_id", "body"],
            },
        ),
        Tool(
            name="gmail_trash_message",
            description="Move a Gmail message to trash.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Gmail message ID to trash"},
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="gmail_mark_read",
            description="Mark a Gmail message as read or unread.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "read": {"type": "boolean", "description": "True to mark as read, False for unread", "default": True},
                },
                "required": ["message_id"],
            },
        ),
        Tool(
            name="gmail_get_profile",
            description="Get the Gmail account profile (email address, total messages, etc.)",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def _dispatch(name: str, args: dict):
    if name == "gmail_get_profile":
        return gmail_request("GET", "/users/me/profile")

    if name == "gmail_list_messages":
        max_r = args.get("max_results", 10)
        label = args.get("label", "INBOX")
        data = gmail_request("GET", f"/users/me/messages", params={"maxResults": max_r, "labelIds": label})
        messages = data.get("messages", [])
        results = []
        for msg in messages:
            detail = gmail_request("GET", f"/users/me/messages/{msg['id']}", params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]})
            headers = parse_message_headers(detail.get("payload", {}).get("headers", []))
            results.append({"id": msg["id"], **headers, "snippet": detail.get("snippet", "")})
        return results

    if name == "gmail_search_messages":
        query = args["query"]
        max_r = args.get("max_results", 10)
        data = gmail_request("GET", "/users/me/messages", params={"q": query, "maxResults": max_r})
        messages = data.get("messages", [])
        results = []
        for msg in messages:
            detail = gmail_request("GET", f"/users/me/messages/{msg['id']}", params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date", "To"]})
            headers = parse_message_headers(detail.get("payload", {}).get("headers", []))
            results.append({"id": msg["id"], **headers, "snippet": detail.get("snippet", "")})
        return results

    if name == "gmail_read_message":
        msg_id = args["message_id"]
        detail = gmail_request("GET", f"/users/me/messages/{msg_id}", params={"format": "full"})
        headers = parse_message_headers(detail.get("payload", {}).get("headers", []))
        body = get_message_body(detail.get("payload", {}))
        return {"id": msg_id, **headers, "body": body, "snippet": detail.get("snippet", "")}

    if name == "gmail_send_message":
        msg = MIMEMultipart()
        msg["To"] = args["to"]
        msg["Subject"] = args["subject"]
        if args.get("cc"):
            msg["Cc"] = args["cc"]
        msg.attach(MIMEText(args["body"], "plain", "utf-8"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return gmail_request("POST", "/users/me/messages/send", json={"raw": raw})

    if name == "gmail_reply_message":
        msg_id = args["message_id"]
        original = gmail_request("GET", f"/users/me/messages/{msg_id}", params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Message-ID", "References"]})
        orig_headers = {h["name"]: h["value"] for h in original.get("payload", {}).get("headers", [])}
        thread_id = original.get("threadId")

        reply = MIMEText(args["body"], "plain", "utf-8")
        reply["To"] = orig_headers.get("From", "")
        reply["Subject"] = "Re: " + orig_headers.get("Subject", "")
        if orig_headers.get("Message-ID"):
            reply["In-Reply-To"] = orig_headers["Message-ID"]
            reply["References"] = orig_headers.get("References", "") + " " + orig_headers["Message-ID"]

        raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
        return gmail_request("POST", "/users/me/messages/send", json={"raw": raw, "threadId": thread_id})

    if name == "gmail_trash_message":
        return gmail_request("POST", f"/users/me/messages/{args['message_id']}/trash")

    if name == "gmail_mark_read":
        msg_id = args["message_id"]
        if args.get("read", True):
            body = {"removeLabelIds": ["UNREAD"]}
        else:
            body = {"addLabelIds": ["UNREAD"]}
        return gmail_request("POST", f"/users/me/messages/{msg_id}/modify", json=body)

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

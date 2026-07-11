#!/usr/bin/env python3
"""
Google Drive MCP Server for Hermes Agent.
Reads OAuth tokens from ~/.config/google-calendar-mcp/tokens.json
Uses GOOGLE_ACCOUNT_MODE env var to select the user account.
"""
import asyncio
import base64
import io
import json
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.path.dirname(__file__))
from token_manager import get_access_token

DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
SHEETS_API = "https://sheets.googleapis.com/v4"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

app = Server("drive-mcp")


def drive_request(method: str, base: str, path: str, **kwargs) -> dict:
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        **kwargs.pop("extra_headers", {}),
    }
    url = f"{base}{path}"
    with httpx.Client() as client:
        resp = client.request(method, url, headers=headers, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def drive(method: str, path: str, **kwargs) -> dict:
    return drive_request(method, DRIVE_API, path, **kwargs)


MIME_EXPORT_MAP = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="drive_list_files",
            description="List files in Google Drive. Shows name, id, mimeType, modifiedTime.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "Max files to return (default 20)", "default": 20},
                    "folder_id": {"type": "string", "description": "Folder ID to list (optional, default is root)"},
                    "order_by": {"type": "string", "description": "Sort order (default: modifiedTime desc)", "default": "modifiedTime desc"},
                },
            },
        ),
        Tool(
            name="drive_search_files",
            description="Search files in Google Drive using Drive query syntax.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query, e.g. \"name contains 'report'\" or \"mimeType='application/pdf'\""},
                    "max_results": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="drive_get_file_content",
            description="Get the text content of a Google Doc, Spreadsheet, or text file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID from Drive"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="drive_get_file_info",
            description="Get metadata about a file (name, size, mimeType, owners, etc.)",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="drive_create_document",
            description="Create a new Google Doc with the given title and content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "content": {"type": "string", "description": "Plain text content"},
                    "folder_id": {"type": "string", "description": "Parent folder ID (optional)"},
                },
                "required": ["title", "content"],
            },
        ),
        Tool(
            name="drive_create_folder",
            description="Create a new folder in Google Drive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Folder name"},
                    "parent_folder_id": {"type": "string", "description": "Parent folder ID (optional)"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="drive_share_file",
            description="Share a Drive file with a specific email address.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID to share"},
                    "email": {"type": "string", "description": "Email address to share with"},
                    "role": {"type": "string", "description": "Role: reader, commenter, or writer", "default": "reader"},
                },
                "required": ["file_id", "email"],
            },
        ),
        Tool(
            name="drive_move_file",
            description="Move a file to a different folder in Google Drive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID to move"},
                    "new_folder_id": {"type": "string", "description": "Target folder ID"},
                },
                "required": ["file_id", "new_folder_id"],
            },
        ),
        Tool(
            name="drive_delete_file",
            description="Move a file to Google Drive trash.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID to trash"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="drive_get_storage_quota",
            description="Get Google Drive storage usage and quota.",
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
    if name == "drive_get_storage_quota":
        info = drive("GET", "/about", params={"fields": "storageQuota,user"})
        quota = info.get("storageQuota", {})
        return {
            "user": info.get("user", {}).get("emailAddress"),
            "used_gb": round(int(quota.get("usage", 0)) / 1e9, 3),
            "limit_gb": round(int(quota.get("limit", 0)) / 1e9, 3) if "limit" in quota else "unlimited",
        }

    if name == "drive_list_files":
        max_r = args.get("max_results", 20)
        order_by = args.get("order_by", "modifiedTime desc")
        params = {
            "pageSize": max_r,
            "orderBy": order_by,
            "fields": "files(id,name,mimeType,modifiedTime,size,parents,webViewLink)",
        }
        if args.get("folder_id"):
            params["q"] = f"'{args['folder_id']}' in parents and trashed=false"
        else:
            params["q"] = "trashed=false"
        return drive("GET", "/files", params=params).get("files", [])

    if name == "drive_search_files":
        query = args["query"]
        max_r = args.get("max_results", 20)
        # Auto-add trashed=false if not specified
        if "trashed" not in query:
            query = f"({query}) and trashed=false"
        return drive("GET", "/files", params={
            "q": query,
            "pageSize": max_r,
            "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
        }).get("files", [])

    if name == "drive_get_file_info":
        file_id = args["file_id"]
        return drive("GET", f"/files/{file_id}", params={
            "fields": "id,name,mimeType,size,modifiedTime,createdTime,owners,webViewLink,parents,description"
        })

    if name == "drive_get_file_content":
        file_id = args["file_id"]
        file_info = drive("GET", f"/files/{file_id}", params={"fields": "mimeType,name"})
        mime = file_info.get("mimeType", "")

        token = get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        with httpx.Client() as client:
            if mime in MIME_EXPORT_MAP:
                export_mime = MIME_EXPORT_MAP[mime]
                resp = client.get(
                    f"{DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": export_mime},
                    headers=headers,
                    timeout=30,
                )
            else:
                resp = client.get(
                    f"{DRIVE_API}/files/{file_id}",
                    params={"alt": "media"},
                    headers=headers,
                    timeout=30,
                )
            resp.raise_for_status()
            content = resp.text[:10000]  # limit to 10k chars

        return {"name": file_info.get("name"), "mimeType": mime, "content": content}

    if name == "drive_create_document":
        title = args["title"]
        content = args["content"]

        # Upload as plain text, then it becomes a regular file
        # To create a Google Doc, we upload and convert
        token = get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        metadata = {
            "name": title,
            "mimeType": "application/vnd.google-apps.document",
        }
        if args.get("folder_id"):
            metadata["parents"] = [args["folder_id"]]

        with httpx.Client() as client:
            resp = client.post(
                f"{UPLOAD_API}/files?uploadType=multipart",
                headers={**headers, "Content-Type": "multipart/related; boundary=boundary"},
                content=(
                    b"--boundary\r\n"
                    b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                    + json.dumps(metadata).encode()
                    + b"\r\n--boundary\r\n"
                    b"Content-Type: text/plain; charset=UTF-8\r\n\r\n"
                    + content.encode("utf-8")
                    + b"\r\n--boundary--"
                ),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    if name == "drive_create_folder":
        metadata = {
            "name": args["name"],
            "mimeType": "application/vnd.google-apps.folder",
        }
        if args.get("parent_folder_id"):
            metadata["parents"] = [args["parent_folder_id"]]
        return drive("POST", "/files", json=metadata)

    if name == "drive_share_file":
        file_id = args["file_id"]
        role = args.get("role", "reader")
        permission = {"type": "user", "role": role, "emailAddress": args["email"]}
        return drive("POST", f"/files/{file_id}/permissions", json=permission)

    if name == "drive_move_file":
        file_id = args["file_id"]
        new_folder_id = args["new_folder_id"]
        # Get current parents
        info = drive("GET", f"/files/{file_id}", params={"fields": "parents"})
        old_parents = ",".join(info.get("parents", []))
        return drive(
            "PATCH",
            f"/files/{file_id}",
            params={"addParents": new_folder_id, "removeParents": old_parents, "fields": "id,name,parents"},
        )

    if name == "drive_delete_file":
        return drive("PATCH", f"/files/{args['file_id']}", json={"trashed": True}, params={"fields": "id,name,trashed"})

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

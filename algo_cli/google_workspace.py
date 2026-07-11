"""Google Workspace REST client.

Read/write access to Drive, Docs, Sheets, and Calendar plus Gmail read/draft
access over HTTPS using only the standard library (urllib.request /
urllib.parse). Mirrors the zero deps posture of google_workspace_auth.py.

The client auto-refreshes access tokens via google_workspace_auth and raises
GoogleWorkspaceError on non-2xx responses. Methods return parsed JSON dicts
or raw bytes for media downloads; they never write to disk.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import formatdate
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import google_workspace_auth as auth

API_BASE = "https://www.googleapis.com"
DRIVE_BASE = f"{API_BASE}/drive/v3"
DOCS_BASE = f"{API_BASE}/documents/v1"
SHEETS_BASE = f"{API_BASE}/spreadsheets/v4"
CALENDAR_BASE = f"{API_BASE}/calendar/v3"
GMAIL_BASE = f"{API_BASE}/gmail/v1"


class GoogleWorkspaceError(RuntimeError):
    """Raised on a non-2xx Google API response."""

    def __init__(self, status: int, detail: str, *, body: dict[str, Any] | None = None):
        super().__init__(f"Google API HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body or {}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GoogleWorkspaceClient:
    """Thin urllib wrapper around Google Workspace REST APIs."""

    def __init__(self) -> None:
        self.last_user_info: dict[str, Any] | None = None

    # -- HTTP plumbing ----------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        accept: str = "application/json",
        timeout: float = 30.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        if params:
            # Drop empty values so we don't pollute URLs with `key=`.
            cleaned = {k: v for k, v in params.items() if v not in (None, "")}
            if cleaned:
                url = f"{url}?{urllib.parse.urlencode(cleaned, doseq=True)}"
        token = auth.get_valid_token()
        if not token:
            raise GoogleWorkspaceError(
                401,
                "Not authenticated. Run /google-login to start the OAuth flow.",
            )
        req_headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
        }
        if headers:
            req_headers.update(headers)
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif raw_body is not None:
            data = raw_body
        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read() or b""
            try:
                body_obj = json.loads(body_bytes.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                body_obj = None
            detail = body_obj.get("error", {}).get("message") if isinstance(body_obj, dict) else None
            raise GoogleWorkspaceError(
                exc.code,
                detail or body_bytes.decode("utf-8", errors="replace") or exc.reason,
                body=body_obj if isinstance(body_obj, dict) else None,
            ) from exc
        except urllib.error.URLError as exc:
            raise GoogleWorkspaceError(0, f"Network error: {exc.reason}") from exc

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        status, body, _ = self._request("GET", url, params=params, timeout=timeout)
        if not (200 <= status < 300):
            detail = body.decode("utf-8", errors="replace")
            raise GoogleWorkspaceError(status, detail)
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise GoogleWorkspaceError(status, f"Non-JSON response: {body!r}") from exc

    def _get_bytes(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "*/*",
        timeout: float = 60.0,
    ) -> tuple[bytes, dict[str, str]]:
        status, body, headers = self._request(
            "GET", url, params=params, accept=accept, timeout=timeout
        )
        if not (200 <= status < 300):
            detail = body.decode("utf-8", errors="replace")
            raise GoogleWorkspaceError(status, detail)
        return body, headers

    # -- Identity ---------------------------------------------------------

    def user_info(self) -> dict[str, Any]:
        info = self._get_json(auth.GOOGLE_USERINFO_URL)
        self.last_user_info = info
        return info

    # -- Drive ------------------------------------------------------------

    def drive_list(
        self,
        *,
        query: str | None = None,
        page_size: int = 20,
        fields: str = "files(id,name,mimeType,size,modifiedTime,webViewLink),nextPageToken",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageSize": max(1, min(int(page_size), 1000)),
            "fields": fields,
        }
        if query:
            params["q"] = query
        return self._get_json(f"{DRIVE_BASE}/files", params=params)

    def drive_get(
        self,
        file_id: str,
        *,
        fields: str = "id,name,mimeType,size,modifiedTime,createdTime,webViewLink,owners,parents,description",
    ) -> dict[str, Any]:
        return self._get_json(
            f"{DRIVE_BASE}/files/{urllib.parse.quote(file_id, safe='')}",
            params={"fields": fields, "supportsAllDrives": "true"},
        )

    def drive_search(
        self,
        name_contains: str,
        *,
        mime_type: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        # Escape single quotes for the Drive query language.
        safe = name_contains.replace("\\", "\\\\").replace("'", "\\'")
        clauses = [f"name contains '{safe}'", "trashed = false"]
        if mime_type:
            clauses.append(f"mimeType = '{mime_type}'")
        return self.drive_list(query=" and ".join(clauses), page_size=page_size)

    def drive_download(self, file_id: str) -> tuple[bytes, dict[str, str]]:
        return self._get_bytes(
            f"{DRIVE_BASE}/files/{urllib.parse.quote(file_id, safe='')}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )

    def drive_export(
        self,
        file_id: str,
        *,
        mime_type: str = "text/plain",
    ) -> tuple[bytes, dict[str, str]]:
        return self._get_bytes(
            f"{DRIVE_BASE}/files/{urllib.parse.quote(file_id, safe='')}/export",
            params={"mimeType": mime_type},
        )

    # -- Docs -------------------------------------------------------------

    def docs_get(self, document_id: str) -> dict[str, Any]:
        return self._get_json(
            f"{DOCS_BASE}/documents/{urllib.parse.quote(document_id, safe='')}"
        )

    def docs_to_plain_text(self, document: dict[str, Any]) -> str:
        """Flatten a docs.get response into a plain-text body for display."""
        body = document.get("body") or {}
        chunks: list[str] = []

        def walk(element: dict[str, Any]) -> None:
            for child in element.get("content", []) or []:
                kind = child.get("type")
                if kind == "paragraph":
                    walk(child)
                    chunks.append("")
                elif kind == "table":
                    for row in child.get("tableRows", []) or []:
                        for cell in row.get("tableCells", []) or []:
                            walk(cell)
                        chunks.append("")
                elif kind == "textRun":
                    content = (child.get("textRun") or {}).get("content", "")
                    if content:
                        chunks.append(content)

        walk(body)
        return "\n".join(chunks).rstrip() + "\n"

    # -- Sheets -----------------------------------------------------------

    def sheets_values_get(
        self,
        spreadsheet_id: str,
        range_a1: str,
    ) -> dict[str, Any]:
        return self._get_json(
            f"{SHEETS_BASE}/spreadsheets/{urllib.parse.quote(spreadsheet_id, safe='')}"
            f"/values/{urllib.parse.quote(range_a1, safe='')}"
        )

    # -- Calendar ---------------------------------------------------------

    def calendar_events_list(
        self,
        *,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "maxResults": max(1, min(int(max_results), 250)),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        return self._get_json(f"{CALENDAR_BASE}/calendars/primary/events", params=params)

    # -- Gmail ------------------------------------------------------------

    def gmail_list(
        self,
        *,
        query: str | None = None,
        max_results: int = 20,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"maxResults": max(1, min(int(max_results), 500))}
        if query:
            params["q"] = query
        return self._get_json(f"{GMAIL_BASE}/users/me/messages", params=params)

    def gmail_get(
        self,
        message_id: str,
        *,
        fmt: str = "metadata",
    ) -> dict[str, Any]:
        allowed = {"metadata", "full", "raw", "minimal"}
        if fmt not in allowed:
            raise ValueError(f"fmt must be one of {sorted(allowed)}")
        return self._get_json(
            f"{GMAIL_BASE}/users/me/messages/{urllib.parse.quote(message_id, safe='')}",
            params={"format": fmt},
        )

    def gmail_create_draft(
        self,
        *,
        to: str,
        subject: str,
        html_body: str | None = None,
        text_body: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict[str, Any]:
        """Create a Gmail draft. Does not send mail."""
        if not to.strip():
            raise ValueError("to is required")
        if not subject.strip():
            raise ValueError("subject is required")
        body_text = text_body or ""
        body_html = html_body or ""
        if not body_text and not body_html:
            raise ValueError("html_body or text_body is required")

        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = "me"
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        if body_html:
            msg.set_content(body_text or "This email contains an HTML body.")
            msg.add_alternative(body_html, subtype="html")
        else:
            msg.set_content(body_text)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        status, body, _headers = self._request(
            "POST",
            f"{GMAIL_BASE}/users/me/drafts",
            json_body={"message": {"raw": raw}},
            timeout=60.0,
        )
        if not (200 <= status < 300):
            detail = body.decode("utf-8", errors="replace")
            raise GoogleWorkspaceError(status, detail)
        return json.loads(body.decode("utf-8") or "{}")


# ---------------------------------------------------------------------------
# Convenience formatters for slash-command display
# ---------------------------------------------------------------------------


def format_drive_files(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in payload.get("files", []) or []:
        if not isinstance(item, dict):
            continue
        fid = item.get("id", "?")
        name = item.get("name", "(unnamed)")
        mime = item.get("mimeType", "")
        size = item.get("size")
        size_s = f"  {int(size):,}B" if size else ""
        lines.append(f"  - {name}  [muted]({mime}{size_s})[/]  id={fid}")
    if not lines:
        lines.append("  (no files)")
    return lines


def format_docs_plain_text(document: dict[str, Any], client: GoogleWorkspaceClient) -> str:
    return client.docs_to_plain_text(document)


def format_sheet_values(payload: dict[str, Any]) -> str:
    rows = payload.get("values", []) or []
    if not rows:
        return "(empty range)"
    width = max(len(row) for row in rows)
    out: list[str] = []
    for row in rows:
        padded = list(row) + [""] * (width - len(row))
        out.append("  | ".join(str(cell) for cell in padded))
    return "\n".join(out)


def format_calendar_events(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in payload.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary", "(no title)")
        start = (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date")
        end = (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date")
        eid = item.get("id", "")
        when = f"{start} → {end}" if start and end else (start or "?")
        lines.append(f"  - {summary}  [muted]({when})[/]  id={eid}")
    if not lines:
        lines.append("  (no events)")
    return lines


def format_gmail_message(message: dict[str, Any]) -> str:
    headers = message.get("payload", {}).get("headers", []) or []
    by_name = {h.get("name", "").lower(): h.get("value", "") for h in headers if isinstance(h, dict)}
    subject = by_name.get("subject", "(no subject)")
    frm = by_name.get("from", "?")
    date = by_name.get("date", "?")
    snippet = message.get("snippet", "")
    return f"  Subject: {subject}\n  From:    {frm}\n  Date:    {date}\n  ID:      {message.get('id', '?')}\n  Snippet: {snippet}"

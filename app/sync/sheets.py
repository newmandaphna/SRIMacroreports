"""Reading tabular data from a source workbook.

Two implementations behind one protocol:

  GoogleSheetsClient reads the real quarterly Q sheet through a service account.
  DemoSheetsClient serves a bundled synthetic workbook with obviously fake patients.

The demo client exists so the whole sync path, mapping, validation, alias resolution,
upsert, dry run, rejection review, can be exercised end to end with no credentials and
no PHI. That is what the build specification asks for, and it also means the engine is
covered by tests that do not touch the network.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from app.models.data_source import BLOCKED_TAB_PREFIX

logger = logging.getLogger(__name__)

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)

_SPREADSHEET_ID_PATTERNS = (
    re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9-_]+)"),
)

# Enough for a full quarter with headroom. A quarter of the real sheet is about 9,400
# rows, so this leaves room to grow without reading an unbounded range.
MAX_ROWS = 50_000


class SheetsError(RuntimeError):
    """Anything that stopped us reading the source. Message is safe to show an admin."""


@dataclass(frozen=True)
class SheetData:
    headers: list[str]
    # Rows are aligned to headers, padded with None where the sheet was ragged.
    rows: list[list[object]]
    # 1 based sheet row number for each row, so a rejection can name where to look.
    row_numbers: list[int]


class SheetsClient(Protocol):
    def list_tabs(self, spreadsheet_id: str) -> list[str]: ...

    def read_tab(self, spreadsheet_id: str, tab_name: str, header_row: int) -> SheetData: ...


def extract_spreadsheet_id(url_or_id: str) -> str:
    """Accept a full Google Sheets URL or a bare ID.

    Admins paste whatever is in their address bar, which carries `/edit?usp=sharing`
    and sometimes a `#gid=`. Asking them to extract the ID by hand is a step that
    will eventually be done wrong.
    """
    candidate = (url_or_id or "").strip()
    if not candidate:
        raise SheetsError("Enter a Google Sheets URL or spreadsheet ID.")

    for pattern in _SPREADSHEET_ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)

    if "/" in candidate or " " in candidate:
        raise SheetsError(
            "That does not look like a Google Sheets URL. It should contain "
            "/spreadsheets/d/ followed by the sheet ID."
        )
    return candidate


def assert_tab_allowed(tab_name: str) -> None:
    """Refuse the RAW tabs outright.

    They carry dates of birth, home and work emails, phone numbers, and ZIP codes,
    none of which any module needs. This is belt and braces with the column allowlist:
    either control alone would stop a DOB from being imported (SECURITY.md 6.2).
    """
    if tab_name.upper().startswith(BLOCKED_TAB_PREFIX):
        raise SheetsError(
            f"The tab {tab_name!r} holds raw Valant exports, which carry patient dates "
            "of birth, emails, and phone numbers. This application never imports "
            "those. Choose the visit level tab instead."
        )


def selectable_tabs(tabs: list[str]) -> list[str]:
    """Tabs an admin is allowed to pick, with the RAW tabs filtered out."""
    return [t for t in tabs if not t.upper().startswith(BLOCKED_TAB_PREFIX)]


def _align(headers: list[str], row: list[object]) -> list[object]:
    if len(row) < len(headers):
        return list(row) + [None] * (len(headers) - len(row))
    return list(row[: len(headers)])


def build_sheet_data(header_row: int, values: list[list[object]]) -> SheetData:
    """Turn a raw range of values into headers plus aligned rows.

    Shared by both clients so that the ragged row handling, which the real sheet does
    need (Google omits trailing empty cells), is identical in test and production.
    """
    if len(values) < header_row:
        raise SheetsError(
            f"The tab has fewer than {header_row} rows, so there is no header row to read."
        )

    headers = [str(h).strip() if h is not None else "" for h in values[header_row - 1]]
    body = values[header_row:]

    rows = [_align(headers, r) for r in body]
    row_numbers = list(range(header_row + 1, header_row + 1 + len(rows)))
    return SheetData(headers=headers, rows=rows, row_numbers=row_numbers)


class GoogleSheetsClient:
    """Reads a real Google Sheet with a read only service account."""

    def __init__(self, service_account_json: str | None) -> None:
        if not service_account_json:
            raise SheetsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not set, so the application cannot "
                "reach Google Sheets. Add it in Replit Secrets (see the README) and "
                "restart."
            )
        self._raw_credentials = service_account_json
        self._service = None

    def _client(self):  # pragma: no cover - requires network and credentials
        if self._service is not None:
            return self._service

        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise SheetsError("The Google API client libraries are not installed.") from exc

        try:
            info = json.loads(self._raw_credentials)
        except json.JSONDecodeError as exc:
            # Deliberately does not include the value: it is a private key.
            raise SheetsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the whole "
                "downloaded key file as the secret's value."
            ) from exc

        credentials = Credentials.from_service_account_info(info, scopes=list(SCOPES))
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    def list_tabs(self, spreadsheet_id: str) -> list[str]:  # pragma: no cover - network
        try:
            meta = (
                self._client()
                .spreadsheets()
                .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
                .execute()
            )
        except Exception as exc:
            raise SheetsError(_friendly_api_error(exc)) from exc

        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def read_tab(
        self, spreadsheet_id: str, tab_name: str, header_row: int
    ) -> SheetData:  # pragma: no cover - network
        assert_tab_allowed(tab_name)
        rng = f"'{tab_name}'!A1:ZZ{MAX_ROWS}"
        try:
            response = (
                self._client()
                .spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=rng,
                    valueRenderOption="UNFORMATTED_VALUE",
                    dateTimeRenderOption="FORMATTED_STRING",
                )
                .execute()
            )
        except Exception as exc:
            raise SheetsError(_friendly_api_error(exc)) from exc

        return build_sheet_data(header_row, response.get("values", []))


def _friendly_api_error(exc: Exception) -> str:  # pragma: no cover - network paths
    """Translate a Google API failure into something an admin can act on.

    Never includes the exception's raw body, which can echo request content.
    """
    text = str(exc)
    if "404" in text:
        return (
            "That spreadsheet was not found. Check the URL, and check that the sheet "
            "is in the Drive folder shared with the service account."
        )
    if "403" in text:
        if "SERVICE_DISABLED" in text or "has not been used" in text:
            return (
                "The Google Sheets API is not enabled in the service account's Cloud "
                "project. Visit the Google Cloud Console → APIs & Services → Enable "
                "APIs and enable 'Google Sheets API' (and 'Google Drive API') for that "
                "project, then try again."
            )
        return (
            "Access denied by Google. Make sure the spreadsheet (or its containing "
            "Drive folder) is shared with the service account address as Viewer."
        )
    if "429" in text or "quota" in text.lower():
        return "Google rate limited the request. Wait a minute and try again."
    logger.exception("Google Sheets API call failed")
    return (
        "Could not read the spreadsheet. The error has been logged; ask an "
        "administrator to check the application log."
    )


class DemoSheetsClient:
    """Serves a bundled synthetic workbook.

    Patients are Patient AA, Patient AB, and so on, with codes PATAA, PATAB. Nothing
    here could be mistaken for a real person. The header row mirrors the real Q2
    Snapshot exactly, including the two unnamed columns and the fact that Patient Code
    sits after Total balance rather than where the specification lists it, so the
    mapping being exercised is the mapping the real sheet needs.
    """

    TAB_NAME = "Q2 Snapshot (demo)"

    def __init__(self) -> None:
        from app.sync.demo_data import DEMO_TABS

        self._tabs = DEMO_TABS

    def list_tabs(self, spreadsheet_id: str) -> list[str]:
        return list(self._tabs)

    def read_tab(self, spreadsheet_id: str, tab_name: str, header_row: int) -> SheetData:
        assert_tab_allowed(tab_name)
        if tab_name not in self._tabs:
            raise SheetsError(
                f"The demo workbook has no tab named {tab_name!r}. "
                f"Available: {', '.join(self._tabs)}."
            )
        return build_sheet_data(header_row, self._tabs[tab_name])


def client_for(provider: str, service_account_json: str | None = None) -> SheetsClient:
    from app.models.data_source import SourceProvider

    if provider == SourceProvider.DEMO:
        return DemoSheetsClient()
    return GoogleSheetsClient(service_account_json)

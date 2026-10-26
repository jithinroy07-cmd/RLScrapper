"""
In-memory stand-in for the slice of gspread 6.2.1 that src/gsheet_manager.py calls.

WHY it exists: the sheet layer is only worth testing against something that
refuses what Google refuses. A permissive fake would let a bug like "write past the
end of the grid" or "delete every non-frozen row" pass here and fail on the first
real run. So this fake is deliberately strict about the rules that have bitten
sheet code, and it is checked against the real library's signatures in
tests/test_gsheet_manager.py.

What it models (each point is a rule the real service enforces):
  * update() past the grid -> APIError 400 "exceeds grid limits"; no auto-grow.
  * A single-cell range is a start cell; a range smaller than the data -> 400.
  * Values must be JSON scalars (str / int / float / bool); None, lists-in-cells,
    Decimal and friends raise, as they do when requests serialises the body.
  * A cell longer than 50,000 characters -> 400. The spreadsheet-wide 10,000,000
    cell cap applies to add_worksheet and resize (`cell_limit` is adjustable).
  * RAW stores text exactly as given. USER_ENTERED is approximated (numbers lose
    leading zeros, ISO dates are re-formatted, "=x" becomes a formula error) so a
    switch to USER_ENTERED changes what tests read back.
  * get_all_values() is rectangular and trimmed to the last non-blank row and
    column, like gspread with pad_values=True; row_values() is ragged and trimmed.
  * append_rows() finds the "table" at table_range (the contiguous run of
    non-blank rows) and writes after it: INSERT_ROWS inserts (rows below shift
    down), the API default OVERWRITE writes over whatever is below. A blank row in
    the middle of the data therefore makes appends land mid-sheet, as on Google.
  * delete_rows() refuses a range outside the grid and refuses to leave no
    non-frozen row ("You can't delete all the non-frozen rows").
  * Duplicate tab titles and removing the last tab are rejected.

Fault injection (the point of the exercise): client.inject("update", 429, times=2)
raises a real gspread APIError with that HTTP status on the matching calls.
`applied=True` performs the operation first and then raises, which is what a
5xx or a timeout after the server already did the work looks like. `exception=`
raises any exception instead (a ConnectionError, or a RuntimeError standing in for
a process crash). `skip=n` lets the first n matching calls through.

The call log (client.calls) records every API call so tests can assert on order
and on what was sent, without mocks.

Everything here is SYNTHETIC: titles, ids and e-mail addresses are obviously fake.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional, Sequence

import requests
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from gspread.utils import GridRangeType, a1_to_rowcol

MAX_CELL_CHARS = 50_000
DEFAULT_CELL_LIMIT = 10_000_000
DEFAULT_SA_EMAIL = "fake-writer@fake-project.iam.gserviceaccount.com"  # SYNTHETIC

_STATUS_NAMES = {
    400: "INVALID_ARGUMENT", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 408: "DEADLINE_EXCEEDED",
    429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "UNAVAILABLE", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED",
}
_DEFAULT_MESSAGES = {
    403: "The caller does not have permission",
    404: "Requested entity was not found.",
    429: "Quota exceeded for quota metric 'Write requests' and limit 'Write requests per minute per user'",
    500: "Internal error encountered.",
    502: "Bad gateway",
    503: "The service is currently unavailable.",
}
_PERM_TYPES = frozenset({"user", "group", "domain", "anyone"})
_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"})


def make_api_error(
    status: int, message: Optional[str] = None, *, reason: str = "", domain: str = "", url: str = ""
) -> APIError:
    """A real gspread APIError, built the way gspread builds one from a Google error response."""
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response.url = url or "https://sheets.googleapis.com/v4/spreadsheets/fake-spreadsheet-id"
    error: dict[str, Any] = {
        "code": status,
        "message": message if message is not None else _DEFAULT_MESSAGES.get(status, "fake error"),
        "status": _STATUS_NAMES.get(status, "UNKNOWN"),
    }
    if reason:
        error["errors"] = [{"message": error["message"], "domain": domain or "usageLimits", "reason": reason}]
    response._content = json.dumps({"error": error}).encode("utf-8")
    response.headers["Content-Type"] = "application/json; charset=UTF-8"
    return APIError(response)


def make_garbled_api_error(status: int, body: str = "<html>Bad Gateway</html>") -> APIError:
    """The APIError gspread builds when the server's error body is not JSON (a proxy page, a truncated reply)."""
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = body.encode("utf-8")
    return APIError(response)


@dataclass
class Call:
    """One API call as the fake saw it."""

    method: str
    tab: Optional[str]
    detail: dict[str, Any]


@dataclass
class Fault:
    """A scheduled failure. See FakeClient.inject."""

    method: str
    make_error: Callable[[], BaseException]
    times: Optional[int] = 1
    tab: Optional[str] = None
    skip: int = 0
    applied: bool = False
    seen: int = 0
    fired: int = 0

    def matches(self, method: str, tab: Optional[str]) -> bool:
        if self.method not in ("*", method):
            return False
        return self.tab is None or self.tab == tab

    def should_fire(self) -> bool:
        self.seen += 1
        if self.seen <= self.skip:
            return False
        return self.times is None or self.fired < self.times


# ---------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------
def _reject(message: str) -> APIError:
    return make_api_error(400, message)


def _as_text(value: Any) -> str:
    """A JSON scalar as the text Sheets would hold for it (RAW)."""
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("Out of range float values are not JSON compliant")
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _user_entered(text: str) -> str:
    """Rough model of Sheets' USER_ENTERED parsing; enough to make a mistaken switch visible."""
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
        number = float(text)
        return str(int(number)) if number.is_integer() else repr(number)
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return f"{int(iso.group(2))}/{int(iso.group(3))}/{iso.group(1)}"
    if text.startswith("="):
        return "#NAME?"
    if text.lower() in ("true", "false"):
        return text.upper()
    return text


def _clean_matrix(values: Any, option: str) -> list[list[str]]:
    """Validate a values matrix the way the server would reject it, and return stored text."""
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        raise TypeError("values must be a list of rows")
    out: list[list[str]] = []
    for row in values:
        if isinstance(row, (str, bytes)) or not hasattr(row, "__iter__"):
            raise _reject("Invalid values[0]: list_value. A row must be a list of cells.")
        cells = []
        for value in row:
            text = _as_text(value)
            if len(text) > MAX_CELL_CHARS:
                raise _reject(
                    f"This action would increase the number of cells in the workbook above the limit, or a cell "
                    f"exceeds {MAX_CELL_CHARS} characters."
                )
            cells.append(_user_entered(text) if option == "USER_ENTERED" else text)
        out.append(cells)
    json.dumps(values)  # what requests does to the body; raises TypeError on anything else
    return out


def _option(value: Any) -> str:
    text = str(getattr(value, "value", value))
    if text not in ("RAW", "USER_ENTERED"):
        raise _reject(f"Invalid value at 'value_input_option': {text!r}")
    return text


def _blank(row: Sequence[str]) -> bool:
    return not any(cell != "" for cell in row)


# ---------------------------------------------------------------------------------
# Worksheet
# ---------------------------------------------------------------------------------
class FakeWorksheet:
    """gspread.Worksheet: the methods and attributes sheet code touches."""

    def __init__(self, spreadsheet: "FakeSpreadsheet", sheet_id: int, title: str, rows: int, cols: int, index: int):
        self.spreadsheet = spreadsheet
        self.id = sheet_id
        self.title = title
        self._rows_count = rows
        self._cols_count = cols
        self.frozen_row_count = 0
        self._index = index
        self._data: list[list[str]] = []  # ragged; row 1 is _data[0]

    # -- properties gspread exposes ------------------------------------------------
    @property
    def index(self) -> int:
        return self._index

    @property
    def row_count(self) -> int:
        return self._rows_count

    @property
    def col_count(self) -> int:
        return self._cols_count

    @property
    def _client(self) -> "FakeClient":
        return self.spreadsheet.client

    def _run(self, method: str, detail: dict[str, Any], operation: Callable[[], Any]) -> Any:
        return self._client._run(method, self.title, detail, operation)

    # -- test helpers (never logged, never faulted) --------------------------------
    def put(self, rows: Iterable[Sequence[Any]], *, start_row: int = 1) -> "FakeWorksheet":
        """Place text rows directly (test set-up), growing the grid to fit."""
        matrix = _clean_matrix([list(r) for r in rows], "RAW")
        self._write(start_row, 1, matrix, grow=True)
        return self

    def dump(self) -> list[list[str]]:
        """What get_all_values() would return, without logging or faults."""
        return self._values()

    def used_rows(self) -> int:
        return len(self._values())

    # -- internals --------------------------------------------------------------------
    def _values(self) -> list[list[str]]:
        last = len(self._data)
        while last and _blank(self._data[last - 1]):
            last -= 1
        rows = [list(r) for r in self._data[:last]]
        width = 0
        for row in rows:
            n = len(row)
            while n and row[n - 1] == "":
                n -= 1
            width = max(width, n)
        return [(r + [""] * width)[:width] for r in rows]

    def _write(self, row: int, col: int, matrix: list[list[str]], *, grow: bool = False) -> None:
        height = len(matrix)
        width = max((len(r) for r in matrix), default=0)
        if grow:
            self._rows_count = max(self._rows_count, row + height - 1)
            self._cols_count = max(self._cols_count, col + width - 1)
        while len(self._data) < row + height - 1:
            self._data.append([])
        for offset, cells in enumerate(matrix):
            target = self._data[row - 1 + offset]
            if len(target) < col - 1 + len(cells):
                target.extend([""] * (col - 1 + len(cells) - len(target)))
            target[col - 1 : col - 1 + len(cells)] = cells

    def _table_end(self, anchor_row: int) -> int:
        """Last row of the contiguous non-blank block that contains/starts at anchor_row (0 if none)."""
        row = anchor_row
        if row > len(self._data) or _blank(self._data[row - 1]):
            return anchor_row - 1
        while row < len(self._data) and not _blank(self._data[row]):
            row += 1
        return row

    # -- API ---------------------------------------------------------------------------
    def get_all_values(
        self, range_name: Optional[str] = None, major_dimension: Any = None, value_render_option: Any = None,
        date_time_render_option: Any = None, combine_merged_cells: bool = False, maintain_size: bool = False,
        pad_values: bool = True, return_type: Any = GridRangeType.ListOfLists,
    ) -> list[list[str]]:
        if (range_name is not None or major_dimension is not None or maintain_size or not pad_values
                or return_type != GridRangeType.ListOfLists):
            raise NotImplementedError("the fake implements get_all_values() with default arguments only")
        # gspread's fill_gaps() answers [[]] (not []) when the tab holds no values at all.
        return self._run("get_all_values", {}, lambda: self._values() or [[]])

    def row_values(self, row: int, major_dimension: Any = None, value_render_option: Any = None,
                   date_time_render_option: Any = None) -> list[str]:
        if isinstance(row, bool) or not isinstance(row, int) or row < 1:
            raise TypeError("row must be a positive integer")

        def read() -> list[str]:
            if row > self._rows_count:
                raise _reject(f"Range ('{self.title}'!A{row}:{row}) exceeds grid limits. Max rows: {self._rows_count}")
            cells = list(self._data[row - 1]) if row <= len(self._data) else []
            while cells and cells[-1] == "":
                cells.pop()
            return cells

        return self._run("row_values", {"row": row}, read)

    def update(
        self, values: Any, range_name: Optional[str] = None, raw: bool = True, major_dimension: Any = None,
        value_input_option: Any = None, include_values_in_response: Optional[bool] = None,
        response_value_render_option: Any = None, response_date_time_render_option: Any = None,
    ) -> dict[str, Any]:
        if major_dimension is not None:
            raise NotImplementedError("the fake implements ROWS-major updates only")
        option = _option(value_input_option) if value_input_option else ("RAW" if raw is True else "USER_ENTERED")
        matrix = _clean_matrix(values, option)
        if not range_name:
            raise TypeError("the fake needs an explicit range_name")
        start, _, end = range_name.partition(":")
        row, col = a1_to_rowcol(start)
        height, width = len(matrix), max((len(r) for r in matrix), default=0)
        if end:
            end_row, end_col = a1_to_rowcol(end)
            if height > end_row - row + 1 or width > end_col - col + 1:
                raise _reject(f"Requested writing within range ['{self.title}'!{range_name}], but tried writing to a larger range.")
        detail = {"range": range_name, "rows": height, "cols": width, "option": option, "values": matrix}

        def write() -> dict[str, Any]:
            if height == 0:
                return {"updatedCells": 0}
            if row + height - 1 > self._rows_count or col + width - 1 > self._cols_count:
                raise _reject(
                    f"Range ('{self.title}'!{range_name}) exceeds grid limits. "
                    f"Max rows: {self._rows_count}, max columns: {self._cols_count}"
                )
            self._write(row, col, matrix)
            return {"updatedCells": sum(len(r) for r in matrix)}

        return self._run("update", detail, write)

    def append_rows(
        self, values: Any, value_input_option: Any = "RAW", insert_data_option: Optional[str] = None,
        table_range: Optional[str] = None, include_values_in_response: Optional[bool] = None,
    ) -> dict[str, Any]:
        option = _option(value_input_option)
        matrix = _clean_matrix(values, option)
        mode = str(getattr(insert_data_option, "value", insert_data_option) or "OVERWRITE")
        if mode not in ("OVERWRITE", "INSERT_ROWS"):
            raise _reject(f"Invalid value at 'insert_data_option': {mode!r}")
        anchor_row = a1_to_rowcol(table_range)[0] if table_range else 1
        detail = {"rows": len(matrix), "option": option, "insert": mode, "table_range": table_range, "values": matrix}

        def append() -> dict[str, Any]:
            if not matrix:
                raise _reject("Invalid values: at least one row is required.")
            at = self._table_end(anchor_row) + 1  # first row after the table
            width = max(len(r) for r in matrix)
            if mode == "INSERT_ROWS":
                while len(self._data) < at - 1:
                    self._data.append([])
                self._data[at - 1 : at - 1] = [list(r) for r in matrix]
                self._rows_count += len(matrix)
            else:
                self._rows_count = max(self._rows_count, at + len(matrix) - 1)
                self._write(at, 1, matrix)
            self._cols_count = max(self._cols_count, width)
            self._guard_cell_limit()
            return {"updates": {"updatedRows": len(matrix), "updatedRange": f"'{self.title}'!A{at}"}}

        return self._run("append_rows", detail, append)

    def delete_rows(self, start_index: int, end_index: Optional[int] = None) -> dict[str, Any]:
        end = start_index if end_index is None else end_index
        detail = {"start": start_index, "end": end}

        def delete() -> dict[str, Any]:
            if start_index < 1 or end < start_index or end > self._rows_count:
                raise _reject(
                    f"Invalid requests[0].deleteDimension: Range [{start_index - 1}, {end}) is outside the sheet "
                    f"(rows: {self._rows_count})."
                )
            remaining = self._rows_count - (end - start_index + 1)
            if remaining <= self.frozen_row_count:
                raise _reject("Invalid requests[0].deleteDimension: You can't delete all the non-frozen rows on the sheet.")
            del self._data[start_index - 1 : end]
            self._rows_count = remaining
            return {"replies": [{}]}

        return self._run("delete_rows", detail, delete)

    def clear(self) -> dict[str, Any]:
        def wipe() -> dict[str, Any]:
            self._data = []
            return {"clearedRange": self.title}

        return self._run("clear", {}, wipe)

    def resize(self, rows: Optional[int] = None, cols: Optional[int] = None) -> dict[str, Any]:
        if rows is None and cols is None:
            raise TypeError("Either 'rows' or 'cols' should be specified.")
        detail = {"rows": rows, "cols": cols}

        def resize() -> dict[str, Any]:
            new_rows = self._rows_count if rows is None else rows
            new_cols = self._cols_count if cols is None else cols
            if new_rows < 1 or new_cols < 1:
                raise _reject("Invalid requests[0].updateSheetProperties: gridProperties must be positive.")
            if new_rows <= self.frozen_row_count:
                raise _reject("Invalid requests[0].updateSheetProperties: frozenRowCount must be below rowCount.")
            before = (self._rows_count, self._cols_count)
            self._rows_count, self._cols_count = new_rows, new_cols
            try:
                self._guard_cell_limit()
            except APIError:
                self._rows_count, self._cols_count = before
                raise
            del self._data[new_rows:]
            for r in self._data:
                del r[new_cols:]
            return {"replies": [{}]}

        return self._run("resize", detail, resize)

    def freeze(self, rows: Optional[int] = None, cols: Optional[int] = None) -> dict[str, Any]:
        if rows is None and cols is None:
            raise TypeError("Either 'rows' or 'cols' should be specified.")

        def freeze() -> dict[str, Any]:
            if rows is not None:
                if rows < 0 or rows >= self._rows_count:
                    raise _reject("Invalid requests[0].updateSheetProperties: frozenRowCount must be below rowCount.")
                self.frozen_row_count = rows
            return {"replies": [{}]}

        return self._run("freeze", {"rows": rows, "cols": cols}, freeze)

    def update_index(self, index: int) -> dict[str, Any]:
        def move() -> dict[str, Any]:
            sheets = self.spreadsheet._sheets
            if not 0 <= index < len(sheets):
                raise _reject(f"Invalid requests[0].updateSheetProperties: index {index} is out of range.")
            sheets.remove(self)
            sheets.insert(index, self)
            for position, ws in enumerate(sheets):
                ws._index = position
            return {"replies": [{}]}

        return self._run("update_index", {"index": index}, move)

    def _guard_cell_limit(self) -> None:
        total = sum(ws._rows_count * ws._cols_count for ws in self.spreadsheet._sheets)
        if total > self.spreadsheet.client.cell_limit:
            raise _reject(
                f"This action would increase the number of cells in the workbook above the limit of "
                f"{self.spreadsheet.client.cell_limit} cells."
            )


# ---------------------------------------------------------------------------------
# Spreadsheet
# ---------------------------------------------------------------------------------
class FakeSpreadsheet:
    """gspread.Spreadsheet."""

    def __init__(self, client: "FakeClient", sheet_id: str, title: str):
        self.client = client
        self.id = sheet_id
        self.title = title
        self._sheets: list[FakeWorksheet] = []
        self._next_tab_id = 0
        self.permissions: list[dict[str, Any]] = []

    def _new_tab(self, title: str, rows: int, cols: int, index: Optional[int]) -> FakeWorksheet:
        if any(ws.title == title for ws in self._sheets):
            raise _reject(f'A sheet with the name "{title}" already exists. Please enter another name.')
        if rows < 1 or cols < 1:
            raise _reject("Invalid requests[0].addSheet: gridProperties must be positive.")
        position = len(self._sheets) if index is None else index
        if not 0 <= position <= len(self._sheets):
            raise _reject(f"Invalid requests[0].addSheet: index {position} is out of range.")
        ws = FakeWorksheet(self, self._next_tab_id, title, rows, cols, position)
        self._next_tab_id += 1
        self._sheets.insert(position, ws)
        for i, sheet in enumerate(self._sheets):
            sheet._index = i
        try:
            ws._guard_cell_limit()
        except APIError:
            self._sheets.remove(ws)
            for i, sheet in enumerate(self._sheets):
                sheet._index = i
            raise
        return ws

    def worksheets(self, exclude_hidden: bool = False) -> list[FakeWorksheet]:
        return self.client._run("worksheets", None, {}, lambda: list(self._sheets))

    def worksheet(self, title: str) -> FakeWorksheet:
        def find() -> FakeWorksheet:
            for ws in self._sheets:
                if ws.title == title:
                    return ws
            raise WorksheetNotFound(title)

        return self.client._run("worksheet", title, {}, find)

    def add_worksheet(self, title: str, rows: int, cols: int, index: Optional[int] = None) -> FakeWorksheet:
        detail = {"title": title, "rows": rows, "cols": cols, "index": index}
        return self.client._run("add_worksheet", title, detail, lambda: self._new_tab(title, rows, cols, index))

    def del_worksheet(self, worksheet: FakeWorksheet) -> dict[str, Any]:
        def delete() -> dict[str, Any]:
            if worksheet not in self._sheets:
                raise _reject("Invalid requests[0].deleteSheet: no sheet with that id.")
            if len(self._sheets) == 1:
                raise _reject("Invalid requests[0].deleteSheet: You can't remove all the sheets in a document.")
            self._sheets.remove(worksheet)
            for i, sheet in enumerate(self._sheets):
                sheet._index = i
            return {"replies": [{}]}

        return self.client._run("del_worksheet", worksheet.title, {}, delete)

    def share(self, email_address: Optional[str], perm_type: str, role: str, notify: bool = True,
              email_message: Optional[str] = None, with_link: bool = False) -> dict[str, Any]:
        detail = {"email": email_address, "perm_type": perm_type, "role": role, "notify": notify}

        def grant() -> dict[str, Any]:
            if perm_type not in _PERM_TYPES:
                raise _reject(f"Invalid value at 'permission.type': {perm_type!r}")
            if role not in _ROLES:
                raise _reject(f"Invalid value at 'permission.role': {role!r}")
            if not isinstance(notify, bool):
                raise TypeError("notify must be a bool")
            if perm_type in ("user", "group") and not (isinstance(email_address, str) and "@" in email_address):
                raise _reject("Invalid value at 'permission.emailAddress'.")
            record = {"email": email_address, "type": perm_type, "role": role, "notify": notify}
            self.permissions.append(record)
            return record

        return self.client._run("share", None, detail, grant)

    # -- test helpers -------------------------------------------------------------------
    def tab(self, title: str) -> FakeWorksheet:
        """Look a tab up without logging a call."""
        return next(ws for ws in self._sheets if ws.title == title)

    def titles(self) -> list[str]:
        return [ws.title for ws in self._sheets]


# ---------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------
class FakeClient:
    """gspread.Client (open / open_by_key / create) plus the call log and fault injection."""

    def __init__(self, *, service_account_email: str = DEFAULT_SA_EMAIL):
        self.spreadsheets: dict[str, FakeSpreadsheet] = {}
        self.forbidden_ids: set[str] = set()  # exist, but not shared with the service account
        self.calls: list[Call] = []
        self.faults: list[Fault] = []
        self.cell_limit = DEFAULT_CELL_LIMIT
        self.timeout: Any = None
        self.http_client = SimpleNamespace(auth=SimpleNamespace(service_account_email=service_account_email))
        self._next_id = 0

    # -- fault injection --------------------------------------------------------------
    def inject(
        self, method: str, status: Optional[int] = None, *, exception: Optional[BaseException] = None,
        times: Optional[int] = 1, tab: Optional[str] = None, skip: int = 0, applied: bool = False,
        message: Optional[str] = None, reason: str = "", garbled: bool = False,
    ) -> Fault:
        """
        Make the next `times` matching calls (after `skip` of them pass) fail.
        Give `status` for a Google-style APIError, or `exception` for anything else.
        `applied=True` runs the operation first (the server did the work, the reply was lost).
        `garbled=True` with `status` yields the APIError gspread builds from a non-JSON body.
        """
        if (status is None) == (exception is None):
            raise ValueError("give exactly one of status or exception")
        if exception is not None:
            def make() -> BaseException:
                return exception
        elif garbled:
            def make() -> BaseException:
                return make_garbled_api_error(status)
        else:
            def make() -> BaseException:
                return make_api_error(status, message, reason=reason)
        fault = Fault(method=method, make_error=make, times=times, tab=tab, skip=skip, applied=applied)
        self.faults.append(fault)
        return fault

    def clear_faults(self) -> None:
        self.faults.clear()

    def _run(self, method: str, tab: Optional[str], detail: dict[str, Any], operation: Callable[[], Any]) -> Any:
        self.calls.append(Call(method, tab, detail))
        for fault in self.faults:
            if fault.matches(method, tab) and fault.should_fire():
                fault.fired += 1
                if fault.applied:
                    operation()
                raise fault.make_error()
        return operation()

    # -- call-log helpers -----------------------------------------------------------------
    def calls_to(self, method: str, tab: Optional[str] = None) -> list[Call]:
        return [c for c in self.calls if c.method == method and (tab is None or c.tab == tab)]

    def methods(self) -> list[str]:
        return [c.method for c in self.calls]

    def reset_calls(self) -> None:
        self.calls.clear()

    # -- test set-up --------------------------------------------------------------------------
    def add_spreadsheet(
        self, title: str, *, sheet_id: Optional[str] = None, tabs: Optional[dict[str, list[list[Any]]]] = None
    ) -> FakeSpreadsheet:
        """
        Register a spreadsheet directly. With `tabs` ({title: rows}) it has exactly those
        tabs; without, it has Google's default empty 'Sheet1' (1000 x 26).
        """
        self._next_id += 1
        key = sheet_id or f"fake-spreadsheet-id-{self._next_id:04d}"
        sheet = FakeSpreadsheet(self, key, title)
        if tabs is None:
            sheet._new_tab("Sheet1", 1000, 26, None)
        else:
            for tab_title, rows in tabs.items():
                ws = sheet._new_tab(tab_title, max(len(rows) + 10, 20), 12, None)
                ws.put(rows)
        self.spreadsheets[key] = sheet
        return sheet

    # -- gspread.Client -----------------------------------------------------------------------
    def set_timeout(self, timeout: Any = None) -> None:
        self.timeout = timeout

    def open(self, title: str, folder_id: Optional[str] = None) -> FakeSpreadsheet:
        def find() -> FakeSpreadsheet:
            for key, sheet in self.spreadsheets.items():
                if sheet.title == title and key not in self.forbidden_ids:
                    return sheet
            raise SpreadsheetNotFound(make_api_error(404, "Requested entity was not found."))

        return self._run("open", None, {"title": title}, find)

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        def find() -> FakeSpreadsheet:
            if key in self.forbidden_ids:
                raise PermissionError  # gspread turns an HTTP 403 into a bare PermissionError
            if key not in self.spreadsheets:
                raise SpreadsheetNotFound(make_api_error(404, "Requested entity was not found."))
            return self.spreadsheets[key]

        return self._run("open_by_key", None, {}, find)  # the key is not logged: it is an identifier worth protecting

    def create(self, title: str, folder_id: Optional[str] = None) -> FakeSpreadsheet:
        def make() -> FakeSpreadsheet:
            return self.add_spreadsheet(title)

        return self._run("create", None, {"title": title}, make)

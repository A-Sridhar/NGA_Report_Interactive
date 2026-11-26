"""Interactive status from NGA saved queries (overall + weekly/daily, failure breakdown with HSD data).

On start the script asks for:
  1. an NGA personal access token (skipped if NGA_TOKEN is already set) -> stored in $env:NGA_TOKEN
     for this process,
  2. how many saved queries to report and their ids (the GUID after `queryid=` in the NGA URL),
  3. the cadence - weekly or daily - and a date range (both inclusive):
     weekly  -> one summary table for the whole range (default: previous Sun..Sat work week)
     daily   -> one row per day in the range (default: today)

For each saved query the validation groups are taken from its own
`ValidationGroup/TestGroupName` condition, so any mix of groups works.
When done, the HTML report is opened in the default browser.

Usage:
    python nga_report_interactive.py                      # prompts for everything
    python nga_report_interactive.py --mode weekly --start 2026-11-15 --end 2026-11-21
    python nga_report_interactive.py --mode daily --start 2026-11-24 --end 2026-11-26
    python nga_report_interactive.py --query <guid> --query <guid>   # skip the query prompts
    python nga_report_interactive.py --no-browser         # print the report to the console instead

TLS: install `truststore` (pip install truststore) so the Intel internal CA from the
Windows certificate store is trusted; otherwise certifi rejects nga-prod.laas.icloud.intel.com.
"""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import html
import importlib.util
import os
import re
import subprocess
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# pip name -> import name. requests is mandatory; the rest are installed on demand but tolerated if that fails.
REQUIRED = {"requests": "requests"}
OPTIONAL = {"truststore": "truststore", "requests-kerberos": "requests_kerberos"}


def ensure_packages() -> None:
    missing = [pip for pip, mod in {**REQUIRED, **OPTIONAL}.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"Installing missing packages: {', '.join(missing)}", file=sys.stderr)
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--quiet", "--disable-pip-version-check", *missing]
    try:
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"  pip install failed ({e}); retrying with Intel proxy...", file=sys.stderr)
        env = {**os.environ, "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "http://proxy-dmz.intel.com:912")}
        try:
            subprocess.run(cmd, check=True, env=env)
        except (subprocess.CalledProcessError, OSError) as e2:
            still = [pip for pip, mod in REQUIRED.items() if importlib.util.find_spec(mod) is None]
            if still:
                sys.exit(f"Cannot continue without {', '.join(still)}: {e2}\n  Install manually: pip install {' '.join(still)}")
            print("  optional packages not installed; continuing with reduced functionality.", file=sys.stderr)
    importlib.invalidate_caches()


ensure_packages()

try:
    import truststore  # uses the OS (Windows) cert store, which has the Intel root CA

    truststore.inject_into_ssl()
except ImportError:
    truststore = None

import requests


def check_tls(url: str) -> bool:
    """True if TLS to `url` verifies; otherwise explain the certificate problem and how to fix it."""
    try:
        requests.head(url, timeout=20)
        return True
    except requests.exceptions.SSLError as e:
        print(f"TLS verification failed for {url}:\n  {str(e).splitlines()[-1][:200]}", file=sys.stderr)
        if truststore is None:
            print("  `truststore` is not available, so the Intel root CA in the Windows store isn't used.", file=sys.stderr)
        else:
            print("  The Intel root CA is missing from this machine's Windows certificate store.", file=sys.stderr)
            print("  Fix: install it from http://certificates.intel.com (Intel Root CA / Intel Internal CA bundle),", file=sys.stderr)
            print("       or run `certutil -addstore Root <IntelRootCA.cer>` as administrator.", file=sys.stderr)
        return False
    except requests.RequestException:
        return True  # not a certificate problem; let the real call report it

BASE_PATH = "https://nga-prod.laas.icloud.intel.com"
# External gateway routes (from NGA-generated client scripts): Projects/... and Search/...
QUERY_URL = BASE_PATH + "/Projects/{project}/api/Query/{qid}"
SEARCH_URL = BASE_PATH + "/Search/{project}/odata/v1/TestRunComposite"
PROJECT = "dmr_pv"
# Header variants tried for an NGA personal access token (nga_pat_...); X-NGA-PAT is the one that works.
PAT_HEADER_SCHEMES = [
    lambda t: {"X-NGA-PAT": t},
    lambda t: {"Authorization": f"Bearer {t}"},
    lambda t: {"Authorization": f"PAT {t}"},
    lambda t: {"Authorization": f"Token {t}"},
    lambda t: {"Authorization": "Basic " + base64.b64encode(f":{t}".encode()).decode()},
    lambda t: {"Authorization": "Basic " + base64.b64encode(f"{t}:".encode()).decode()},
    lambda t: {"X-Api-Key": t},
    lambda t: {"PAT": t},
]
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
GROUP_PROPERTY = "ValidationGroup/TestGroupName"
STATES = ["Passed", "Failed", "Aborted", "Canceled", "InProgress", "PendingRunning", "PendingGatekeepers"]

# Saved-query condition entities -> OData navigation path on TestRunComposite
ENTITY_MAP = {"TestRunComposite": "", "TestLineRevision/TestSuite": "TestLine/TestSuite/"}


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted.")


def get_token() -> str:
    token = os.environ.get("NGA_TOKEN", "").strip()
    if not token:
        print("NGA token not found in $env:NGA_TOKEN.")
        token = getpass.getpass("Enter NGA personal access token (input hidden): ").strip()
        if not token:
            sys.exit("No token entered.")
        os.environ["NGA_TOKEN"] = token  # visible to this process only
        print("Token stored in $env:NGA_TOKEN for this run. To keep it for the whole shell session, run:")
        print('  $env:NGA_TOKEN = "<token>"')
    return token.removeprefix("Bearer ").strip()


def ask_query_ids() -> list[str]:
    while True:
        n = ask("How many saved queries to report? ")
        if n.isdigit() and int(n) > 0:
            break
        print("  Enter a positive whole number.")
    ids: list[str] = []
    for i in range(1, int(n) + 1):
        while True:
            raw = ask(f"Query id #{i} (GUID or NGA URL containing queryid=): ")
            m = re.search(r"queryid=([0-9a-f-]{36})", raw, re.I)
            qid = m.group(1) if m else raw
            if GUID_RE.match(qid):
                ids.append(qid.lower())
                break
            print("  Not a valid GUID, e.g. d7b00f43-9b9c-4c80-8d4d-caf99c7c23c4")
    return ids


def group_display_name(group: str) -> str:
    # pv_power_management -> Power Management
    return re.sub(r"^pv_", "", group).replace("_", " ").title()


def ask_mode() -> str:
    while True:
        m = ask("Report cadence - (w)eekly or (d)aily? [w]: ").lower() or "w"
        if m in ("w", "weekly"):
            return "weekly"
        if m in ("d", "daily"):
            return "daily"
        print("  Enter w or d.")


def ask_date(prompt: str, default: date) -> date:
    while True:
        raw = ask(f"{prompt} [YYYY-MM-DD, Enter for {default.isoformat()}]: ")
        if not raw:
            return default
        try:
            return date.fromisoformat(raw)
        except ValueError:
            print("  Invalid date.")


def groups_from_query(query: dict) -> dict[str, str]:
    """Validation groups named in the saved query's own conditions -> display names."""
    for c in query["Conditions"]:
        if c["Property"] == GROUP_PROPERTY:
            return {v: group_display_name(v) for v in c["Values"]}
    return {}


class Nga:
    def __init__(self, token: str, verify: bool):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json"})
        self.s.verify = verify
        self._authenticate(token)

    def _authenticate(self, token: str) -> None:
        url = SEARCH_URL.format(project=PROJECT)
        params = {"$top": "0", "$count": "true"}
        schemes = PAT_HEADER_SCHEMES if token.startswith("nga_pat_") else PAT_HEADER_SCHEMES[:1]
        last: requests.Response | None = None
        for scheme in schemes:
            headers = scheme(token)
            name, value = next(iter(headers.items()))
            label = f"{name}: {value.split(' ')[0] if ' ' in value else '<token>'}"
            last = self.s.get(url, headers=headers, params=params, timeout=60)
            if last.ok:
                self.s.headers.update(headers)
                print(f"  auth ok via {label}", file=sys.stderr)
                return
            print(f"  {label} -> HTTP {last.status_code}", file=sys.stderr)
        hint = last.headers.get("WWW-Authenticate", "") if last is not None else ""
        body = (last.text or "")[:300] if last is not None else ""
        sys.exit(f"Authentication rejected by {url}.\n  WWW-Authenticate: {hint}\n  body: {body}")

    def saved_query(self, query_id: str) -> dict:
        url = QUERY_URL.format(project=PROJECT, qid=query_id)
        r = self.s.get(url, timeout=60)
        if r.status_code == 404:
            sys.exit(f"Saved query {query_id} not found in project {PROJECT} (check the id / your access).")
        r.raise_for_status()
        return r.json()

    def count(self, odata_filter: str) -> int:
        r = self.s.get(
            SEARCH_URL.format(project=PROJECT),
            params={"$filter": odata_filter, "$count": "true", "$top": "0"},
            timeout=120,
        )
        r.raise_for_status()
        return int(r.json()["@odata.count"])

    def rows(self, odata_filter: str, top: int = 200) -> list[dict]:
        # Navigation properties come back only when $expand-ed; Bucket under Failure may not be exposed.
        expands = [
            "TestLine($select=TestLineName),Station($select=StationName),Failure($expand=Bucket($select=BucketName))",
            "TestLine($select=TestLineName),Station($select=StationName),Failure",
        ]
        for i, expand in enumerate(expands):
            r = self.s.get(
                SEARCH_URL.format(project=PROJECT),
                params={"$filter": odata_filter, "$top": str(top), "$orderby": "TestEndDateTime desc",
                        "$expand": expand},
                timeout=120,
            )
            if r.ok:
                body = r.json()
                return body.get("value", body) if isinstance(body, dict) else body
            if r.status_code != 400 or i == len(expands) - 1:
                r.raise_for_status()
        return []


ANSI_RE = re.compile(r"\x1b?\[[0-9;]*[A-Za-z]")
UNIT_RE = re.compile(r"DMR-[A-Z]{2}\d{3}-\d{3}")
BUCKET_IN_DESC_RE = re.compile(r"attached to Bucket (\S+)")
HSD_ID_RE = re.compile(r"(?<!\d)(\d{9,12})(?!\d)")  # e.g. presighting_16032499967 / sighting_16032426202

HSD_API = "https://hsdes-api.intel.com/rest/article/{id}"
HSD_LINK = "https://hsdes.intel.com/appstore/article-one/#/{id}"
HSD_FIELDS = ["status", "title", "owner", "priority", "days_open", "days_lastupdate", "days_closed",
              "days_sighting_submitted", "comments"]
HSD_COLUMNS = {  # field -> report column header
    "status": "HSD status", "title": "HSD title", "owner": "Owner", "priority": "Priority",
    "days_open": "Open Days", "days_lastupdate": "Days Last Update", "days_closed": "Days Closed",
    "days_sighting_submitted": "Days Sighting Submitted", "comment_count": "Comments",
}
HSD_COMMENT_RE = re.compile(r"^\+{4}\d+", re.M)  # each comment in the `comments` blob starts with ++++<id> <user>


class Hsd:
    """HSD-ES article lookup via Windows SSO (Kerberos); failures degrade to blank columns."""

    def __init__(self, verify: bool, enabled: bool = True):
        self.cache: dict[str, dict[str, str]] = {}
        self.s: requests.Session | None = None
        self.warned_401 = False
        if not enabled:
            return
        try:
            from requests_kerberos import DISABLED, HTTPKerberosAuth  # optional dependency
        except ImportError:
            print("Hint: `pip install requests-kerberos` to enrich buckets with HSD-ES data (skipped).", file=sys.stderr)
            return
        self.s = requests.Session()
        self.s.auth = HTTPKerberosAuth(mutual_authentication=DISABLED)
        self.s.headers.update({"Accept": "application/json"})
        self.s.verify = verify

    @staticmethod
    def id_from(bucket: str) -> str:
        m = HSD_ID_RE.search(bucket or "")
        return m.group(1) if m else ""

    def get(self, hsd_id: str) -> dict[str, str]:
        if not hsd_id or self.s is None:
            return {}
        if hsd_id not in self.cache:
            data: dict[str, str] = {}
            try:
                # No `fields` filter: counters are tenant-namespaced (e.g. server_platf.bug.days_open).
                r = self.s.get(HSD_API.format(id=hsd_id), timeout=30)
                if r.ok:
                    rows = r.json().get("data") or []
                    raw = rows[0] if rows else {}
                    for field in HSD_FIELDS:
                        key = field if field in raw else next((k for k in raw if k.endswith("." + field)), None)
                        v = raw.get(key) if key else None
                        data[field] = "" if v is None else str(v)
                    data["comment_count"] = str(len(HSD_COMMENT_RE.findall(data.pop("comments", ""))))
                else:
                    print(f"  HSD {hsd_id} -> HTTP {r.status_code}", file=sys.stderr)
                    if r.status_code == 401 and not self.warned_401:
                        self.warned_401 = True
                        print("  HSD-ES rejected Windows SSO: no Kerberos ticket for the Intel domain? "
                              "Check `klist` for krbtgt/GAR.CORP.INTEL.COM; lock/unlock the PC on VPN to refresh it.",
                              file=sys.stderr)
            except requests.RequestException as e:
                print(f"  HSD {hsd_id} -> {type(e).__name__}", file=sys.stderr)
            self.cache[hsd_id] = data
        return self.cache[hsd_id]


def ci_get(d: dict, key: str, default=None):
    """Case-insensitive dict lookup (OData returns camelCase, metadata uses PascalCase)."""
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return default


def short(text: str, n: int = 90) -> str:
    text = " ".join(text.split()).replace("|", "/")
    return text if len(text) <= n else text[: n - 1] + "…"


def hsd_title_link(title: str, hsd_id: str) -> "Link | str":
    text = " ".join(title.split())
    return Link(text, HSD_LINK.format(id=hsd_id)) if hsd_id and text else text


def failure_record(row: dict) -> dict[str, str]:
    name = ci_get(ci_get(row, "testLine") or {}, "testLineName", "") or ""
    unit = (UNIT_RE.search(name) or [""])[0] if name else ""
    test = re.sub(r"^(\d+-)+", "", name)  # strip leading numeric ids
    test = re.sub(r"_Oak_Stream_Platform.*$", "", test)
    fails = ci_get(row, "failure") or []
    buckets, substates, sightings = [], [], []
    for f in fails:
        b = ci_get(f, "bucket") or {}
        bname = ci_get(b, "bucketName", "") if isinstance(b, dict) else ""
        if not bname:
            m = BUCKET_IN_DESC_RE.search(ci_get(f, "description", "") or "")
            bname = m.group(1) if m else ""
        if bname and bname not in buckets:
            buckets.append(bname)
        ss = ci_get(f, "subState", "") or ""
        if ss and ss not in substates:
            substates.append(ss)
        if ci_get(f, "sightingId"):
            sightings.append(str(ci_get(f, "sightingId")))
    err = ANSI_RE.sub("", ci_get(row, "stateDescription", "") or "")
    err = re.sub(r"\s*Id=[0-9a-f-]{36}", "", err)
    end = (ci_get(row, "testEndDateTime", "") or "")[:16].replace("T", " ")
    return {
        "Test": test,
        "Config": unit,
        "Station": ci_get(ci_get(row, "station") or {}, "stationName", "") or "",
        "Bucket": "; ".join(buckets),
        "SubState": "; ".join(substates),
        "Sighting": "yes" if sightings else "",
        "Error": err,
        "Attempted date": end,
        "Test line": name,
    }


def odata_literal(value: str) -> str:
    if value.lower() in ("true", "false"):
        return value.lower()
    return "'" + value.replace("'", "''") + "'"


def conditions_to_filter(query: dict) -> str:
    parts = []
    for c in query["Conditions"]:
        prop = ENTITY_MAP.get(c["Entity"], c["Entity"] + "/") + c["Property"]
        op, values = c["Operator"].lower(), c["Values"]
        if op == "in":
            parts.append("(" + " or ".join(f"{prop} eq {odata_literal(v)}" for v in values) + ")")
        elif op in ("eq", "ne", "gt", "ge", "lt", "le"):
            parts.append(f"{prop} {op} {odata_literal(values[0])}")
        else:
            raise ValueError(f"Unsupported operator {c['Operator']} in saved query {query['Name']}")
    return " and ".join(parts)


def previous_week() -> tuple[date, date]:
    """Most recent completed Sun..Sat work week as inclusive (start, end) dates."""
    today = date.today()
    this_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    # e.g. run on Thu 2026-11-26 -> (2026-11-15, 2026-11-21)
    return this_sunday - timedelta(days=7), this_sunday - timedelta(days=1)


def intel_ww(d: date) -> str:
    """Intel work week: weeks start Sunday, WW01 is the week containing Jan 1."""

    def ww1_start(year: int) -> date:
        jan1 = date(year, 1, 1)
        return jan1 - timedelta(days=(jan1.weekday() + 1) % 7)

    year = d.year + 1 if d >= ww1_start(d.year + 1) else d.year
    return f"WW{(d - ww1_start(year)).days // 7 + 1:02d}"


def intel_ww_day(d: date) -> str:
    """Intel WW with day index, Sunday = .0, Monday = .1 ... Saturday = .6 (e.g. Thu 2026-11-26 -> WW48.4)."""
    return f"{intel_ww(d)}.{(d.weekday() + 1) % 7}"


def ww_span(start: date, end: date, tag=intel_ww) -> str:
    a, b = tag(start), tag(end)
    return a if a == b else f"{a}-{b}"


def ask_range(mode: str, default_start: date, default_end: date) -> tuple[date, date]:
    while True:
        start = ask_date(f"{mode.capitalize()} report - start date", default_start)
        end = ask_date(f"{mode.capitalize()} report - end date", max(default_end, start))
        if end >= start:
            return start, end
        print("  End date must not be before start date.")


def pct(p: int, f: int) -> str:
    return f"{100 * p / (p + f):.1f}%" if (p + f) else "-"


class Link:
    """Table cell rendered as plain text in the console, a link in Markdown/HTML."""

    def __init__(self, text: str, url: str):
        self.text, self.url = text, url


def cell_text(x: object, mode: str) -> str:
    if isinstance(x, Link):
        if mode == "md":
            t = x.text.replace("|", "/").replace("[", "\\[").replace("]", "\\]")
            return f"[{t}]({x.url})"
        if mode == "html":
            return f'<a href="{html.escape(x.url, quote=True)}">{html.escape(x.text)}</a>'
        if mode == "term":  # OSC 8 hyperlink: clickable title in Windows Terminal / VS Code terminal
            return f"\x1b]8;;{x.url}\x1b\\{x.text.replace('|', '/')}\x1b]8;;\x1b\\"
        return x.text.replace("|", "/")
    s = str(x)
    return html.escape(s) if mode == "html" else s.replace("|", "/")


def visible_len(s: str) -> int:
    return len(re.sub(r"\x1b\]8;;.*?\x1b\\", "", s))


def md_table(headers: list[str], rows: list[list], text_cols: int = 2, mode: str = "text") -> str:
    cells = [list(headers)] + [[cell_text(x, mode) for x in r] for r in rows]
    widths = [max(visible_len(row[i]) for row in cells) for i in range(len(headers))]
    # Leading text columns left-aligned; remaining (numeric) columns right-aligned.
    left = [True] * text_cols + [False] * (len(headers) - text_cols)

    def pad(c: str, w: int, lj: bool) -> str:
        fill = " " * (w - visible_len(c))
        return c + fill if lj else fill + c

    def fmt(row: list[str]) -> str:
        return "| " + " | ".join(pad(c, w, lj) for lj, c, w in zip(left, row, widths)) + " |"

    sep = "|" + "|".join("-" * (w + 2) if lj else "-" * (w + 1) + ":" for lj, w in zip(left, widths)) + "|"
    return "\n".join([fmt(cells[0]), sep, *(fmt(r) for r in cells[1:])])


def html_table(headers: list[str], rows: list[list], text_cols: int = 2) -> str:
    def tr(cells: list, tag: str) -> str:
        return "<tr>" + "".join(
            f'<{tag}{"" if i < text_cols else " class=num"}>{cell_text(c, "html")}</{tag}>' for i, c in enumerate(cells)
        ) + "</tr>"

    body = "".join(tr(r, "td") for r in rows)
    return f"<table>{tr(headers, 'th')}{body}</table>"


Block = tuple  # ("h1"|"h2"|"h3"|"p", text) or ("table", headers, rows, text_cols)


def render(blocks: list[Block], mode: str) -> str:
    out: list[str] = []
    for b in blocks:
        kind = b[0]
        if kind == "table":
            _, headers, rows, text_cols = b
            out.append(html_table(headers, rows, text_cols) if mode == "html" else md_table(headers, rows, text_cols, mode))
        elif mode == "html":
            out.append(f"<{kind}>{html.escape(b[1])}</{kind}>" if kind != "p" else f"<p><em>{html.escape(b[1])}</em></p>")
        else:
            prefix = {"h1": "# ", "h2": "## ", "h3": "### ", "p": "_"}[kind]
            out.append(prefix + b[1] + ("_" if kind == "p" else ""))
        out.append("")
    if mode == "html":
        style = ("<style>body{font-family:Segoe UI,Arial,sans-serif;font-size:13px}table{border-collapse:collapse;"
                 "margin:6px 0 14px}th,td{border:1px solid #bbb;padding:3px 8px;vertical-align:top}th{background:#eee;"
                 "text-align:left}td.num,th.num{text-align:right}</style>")
        return f"<!doctype html><html><head><meta charset='utf-8'>{style}</head><body>{''.join(out)}</body></html>"
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekly", "daily"], help="report cadence; prompts if omitted")
    ap.add_argument("--start", type=date.fromisoformat, help="range start (inclusive); prompts if omitted")
    ap.add_argument("--end", type=date.fromisoformat, help="range end (inclusive); defaults to --start")
    ap.add_argument("--insecure", action="store_true", help="disable TLS verification")
    ap.add_argument("--no-hsd", action="store_true", help="skip HSD-ES lookup for bucket ids")
    ap.add_argument("--no-browser", action="store_true", help="print to console instead of opening the HTML")
    ap.add_argument("--query", action="append", metavar="GUID", help="saved query id (repeatable); prompts if omitted")
    ap.add_argument("--out", type=Path, default=Path("."), help="output folder")
    args = ap.parse_args()

    if args.insecure:
        requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
    elif not check_tls(BASE_PATH):
        if ask("Continue WITHOUT TLS verification for this run? [y/N]: ").lower() in ("y", "yes"):
            args.insecure = True
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]
        else:
            sys.exit("Fix the certificate setup (see above) or rerun with --insecure.")
    token = get_token()
    query_ids = [q.lower() for q in args.query] if args.query else ask_query_ids()
    bad = [q for q in query_ids if not GUID_RE.match(q)]
    if bad:
        sys.exit(f"Invalid query id(s): {', '.join(bad)}")

    mode = args.mode or ask_mode()
    if args.start and args.end:
        start, end = args.start, args.end
    elif args.start or args.end:
        start = args.start or args.end
        end = args.end or args.start
    elif mode == "daily":
        start, end = ask_range(mode, date.today(), date.today())
    else:
        start, end = ask_range(mode, *previous_week())
    if end < start:
        sys.exit("--end must not be before --start.")

    if mode == "daily":
        period_tag = intel_ww_day  # per-row label for failures inside the period
        period_col = "WW.day"
        period_name = ww_span(start, end, intel_ww_day)
        period_title = f"Daily ({period_name}: {start.isoformat()} {start:%a} to {end.isoformat()} {end:%a})"
    else:
        period_tag = intel_ww
        period_col = "WW"
        period_name = ww_span(start, end)
        period_title = f"Weekly ({period_name}: {start.isoformat()} to {end.isoformat()} ({start:%a}-{end:%a}))"
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    nga = Nga(token, verify=not args.insecure)
    hsd = Hsd(verify=not args.insecure, enabled=not args.no_hsd)

    def range_filter(a: date, b: date) -> str:
        return (f"TestEndDateTime ge {a.isoformat()}T00:00:00Z and "
                f"TestEndDateTime lt {(b + timedelta(days=1)).isoformat()}T00:00:00Z")

    week_filter = range_filter(start, end)
    overall_rows, week_rows, daily_rows, csv_rows = [], [], [], []
    failures: list[dict] = []  # one record per failed latest-run test line
    fail_sections: list[Block] = []

    for qid in query_ids:
        query = nga.saved_query(qid)
        label = query["Name"]
        base = conditions_to_filter(query)
        groups = groups_from_query(query) or {"": "All"}  # no group condition -> report the query as a whole
        print(f"[{label}] {base}", file=sys.stderr)
        print(f"  groups: {', '.join(groups.values())}", file=sys.stderr)
        for group, group_name in groups.items():
            g = f"{base} and {GROUP_PROPERTY} eq '{group}'" if group else base
            total = nga.count(g)
            by_state = {s: nga.count(f"{g} and State eq '{s}'") for s in STATES}
            overall_rows.append(
                [label, group_name, total, *by_state.values(), pct(by_state["Passed"], by_state["Failed"])]
            )
            wk_total = nga.count(f"{g} and {week_filter}")
            wk_p = nga.count(f"{g} and {week_filter} and State eq 'Passed'")
            wk_f = nga.count(f"{g} and {week_filter} and State eq 'Failed'")
            week_rows.append([label, group_name, wk_total, wk_p, wk_f, wk_total - wk_p - wk_f, pct(wk_p, wk_f)])
            if mode == "daily":
                for d in days:
                    df = range_filter(d, d)
                    d_total = nga.count(f"{g} and {df}")
                    d_p = nga.count(f"{g} and {df} and State eq 'Passed'") if d_total else 0
                    d_f = nga.count(f"{g} and {df} and State eq 'Failed'") if d_total else 0
                    daily_rows.append([f"{intel_ww_day(d)} ({d.isoformat()} {d:%a})", label, group_name,
                                       d_total, d_p, d_f, d_total - d_p - d_f, pct(d_p, d_f)])
                    csv_rows.append({"query": label, "group": group_name, "scope": d.isoformat(),
                                     "total": d_total, "Passed": d_p, "Failed": d_f})
            csv_rows.append(
                {"query": label, "group": group_name, "scope": "overall", "total": total, **by_state}
            )
            csv_rows.append(
                {"query": label, "group": group_name, "scope": f"{start}..{end}",
                 "total": wk_total, "Passed": wk_p, "Failed": wk_f}
            )

            if by_state["Failed"]:
                recs = []
                for row in nga.rows(f"{g} and State eq 'Failed'"):
                    rec = failure_record(row)
                    attempted = rec["Attempted date"][:10]
                    rec[period_col] = period_tag(date.fromisoformat(attempted)) if attempted and start.isoformat() <= attempted <= end.isoformat() else ""
                    hsd_id = Hsd.id_from(rec["Bucket"])
                    info = hsd.get(hsd_id)
                    rec["HSD"] = hsd_id
                    rec["HSD link"] = HSD_LINK.format(id=hsd_id) if hsd_id else ""
                    for field, col in HSD_COLUMNS.items():
                        rec[col] = info.get(field, "")
                    recs.append(rec)
                    failures.append({"query": label, "group": group_name, **rec})
                hsd_cols = list(HSD_COLUMNS.values())
                fail_sections += [
                    ("h3", f"{label} - {group_name} ({by_state['Failed']} failed, {wk_f} in {period_name})"),
                    ("table",
                     ["Test", "Config", "Station", "Bucket", "Attempted date", period_col, *hsd_cols],
                     [[short(r["Test"], 60), r["Config"], r["Station"], r["Bucket"], r["Attempted date"], r[period_col],
                       r["HSD status"], hsd_title_link(r["HSD title"], r["HSD"]), r["Owner"], r["Priority"],
                       r["Open Days"], r["Days Last Update"], r["Days Closed"], r["Days Sighting Submitted"],
                       r["Comments"]] for r in recs],
                     10),
                ]

    sig_counts: dict[tuple[str, str, str], int] = {}
    for f in failures:
        key = (f["query"], f["group"], f["Bucket"])
        sig_counts[key] = sig_counts.get(key, 0) + 1
    # Sort by query, then group, then descending count.
    sig_rows = []
    for (q, g, s), n in sorted(sig_counts.items(), key=lambda kv: (kv[0][0], kv[0][1], -kv[1], kv[0][2])):
        hsd_id = Hsd.id_from(s)
        info = hsd.get(hsd_id)
        sig_rows.append([q, g, s, info.get("status", ""), hsd_title_link(info.get("title", ""), hsd_id),
                         info.get("owner", ""), n])

    blocks: list[Block] = [
        ("h1", f"NGA status - generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"),
        ("h2", period_title),
    ]
    if mode == "daily":
        blocks.append(("table", ["Day", "Query", "Group", "Executed", "Passed", "Failed", "Other", "Pass %"],
                       daily_rows, 3))
        if len(days) > 1:
            blocks += [("h3", f"Total for {start.isoformat()} to {end.isoformat()}"),
                       ("table", ["Query", "Group", "Executed", "Passed", "Failed", "Other", "Pass %"], week_rows, 2)]
    else:
        blocks.append(("table", ["Query", "Group", "Executed", "Passed", "Failed", "Other", "Pass %"], week_rows, 2))
    blocks += [
        ("h2", "Overall (latest run per test line)"),
        ("table", ["Query", "Group", "Total", *STATES, "Pass % (P/P+F)"], overall_rows, 2),
        ("h2", "Failure breakdown (latest run = Failed)"),
        ("h3", "By bucket"),
        ("table", ["Query", "Group", "Bucket", "HSD status", "HSD title", "Owner", "Count"], sig_rows, 6)
        if sig_rows else ("p", "no failures"),
        *fail_sections,
    ]

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"nga_{mode}_{start.isoformat()}" + (f"_{end.isoformat()}" if end != start else "")
    html_path = args.out / f"{stem}.html"
    (args.out / f"{stem}.md").write_text(render(blocks, "md"), encoding="utf-8")
    html_path.write_text(render(blocks, "html"), encoding="utf-8")
    if failures:
        with (args.out / f"{stem}_failures.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(failures[0]))
            w.writeheader()
            w.writerows(failures)
    with (args.out / f"{stem}.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = ["query", "group", "scope", "total", *STATES]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(csv_rows)
    extra = f", {stem}_failures.csv" if failures else ""
    print(f"\nSaved {stem}.md, {stem}.html, {stem}.csv{extra} in {args.out.resolve()}", file=sys.stderr)
    if args.no_browser or not webbrowser.open(html_path.resolve().as_uri()):
        print(render(blocks, "term" if sys.stdout.isatty() else "text"))
    else:
        print(f"Opened {html_path.name} in your browser.", file=sys.stderr)


if __name__ == "__main__":
    main()

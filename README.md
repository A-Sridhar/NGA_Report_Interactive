# NGA_Report_Interactive

Interactive PM / Reset execution status report from NGA saved queries (project `dmr_pv`).
Prompts for everything, then opens the HTML report in your browser.

What it asks on start:
1. NGA personal access token (skipped if `$env:NGA_TOKEN` is set)
2. Number of saved queries and their ids (GUID after `queryid=` in the NGA URL; the full URL works too)
3. Cadence: **weekly** (one summary table for the range, default previous Sun..Sat work week) or
   **daily** (one row per day, default today) and the date range

Validation groups are taken from each saved query's own `ValidationGroup/TestGroupName` condition.
Failures are enriched from HSD-ES (status, title linked to the article, owner, priority, days_*
counters, comment count) when the bucket name contains an HSD id.

## Requirements

Python 3.10+. Missing packages (`requests`, `truststore`, `requests-kerberos`) are installed
automatically on first run; the script also checks that the Intel root CA is trusted and tells you how
to fix it if not.

## Usage

```powershell
python nga_report_interactive.py                 # prompts for everything, opens HTML in browser
python nga_report_interactive.py --mode weekly --start 2026-11-15 --end 2026-11-21 --query <guid>
python nga_report_interactive.py --mode daily  --start 2026-11-24 --end 2026-11-26 --query <guid> --query <guid>
python nga_report_interactive.py --no-browser    # print to console instead
python nga_report_interactive.py --no-hsd        # skip HSD-ES lookups
python nga_report_interactive.py --out reports   # output folder
```

Outputs `nga_<mode>_<start>[_<end>].md`, `.html`, `.csv` and `_failures.csv`.

## Notes

- NGA PAT is sent as `X-NGA-PAT`. Saved-query conditions come from `/Projects/{project}/api/Query/{id}`;
  counts and rows from `/Search/{project}/odata/v1/TestRunComposite` (navigation properties need `$expand`).
- HSD-ES uses Windows SSO (Kerberos). A 401 usually means the Intel-domain Kerberos ticket is missing
  (`klist`); lock/unlock the PC on VPN to refresh it.
- Work week follows the Intel convention: weeks start Sunday, WW01 contains Jan 1, day index Sun=.0 .. Sat=.6.

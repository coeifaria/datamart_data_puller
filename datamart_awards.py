#!/usr/bin/env python3
"""
datamart_awards.py — deterministic bulk extraction of the CCCCO Data Mart
Program Awards Summary report.

    https://datamart.cccco.edu/outcomes/Program_Awards.aspx

The page is a DevExpress ASPxPivotGrid app (ASPxComboBox, ASPxTreeList,
DXR.axd resources, DXCallback partial postbacks). It has no query-string
API and no JSON endpoint: every parameter change is a server callback that
mutates session state. It therefore cannot be read by any search/extract/
crawl API. It has to be driven by a real browser session.

Design rules (these are what stop the agent-loop failure):
  1. One CSV per (college, year) cell. 15 x 3 = 45 small pulls, so no single
     request ever approaches Data Mart's row cap.
  2. Zero fixed sleeps in the wait path. Waits are event-based: DevExpress
     loading panel hidden + network quiet + expect_download().
  3. Idempotent. A non-empty output file is skipped, so a crash, a session
     timeout, or a killed process resumes instead of restarting.
  4. The script owns all retrying. An agent's only job is to run `pull`,
     then read `status`. It must never drive the browser turn by turn.

Subcommands
-----------
  discover   Dump DevExpress control IDs + the exact District-College and
             Academic Year option strings to ./discovery/. Run ONCE, then
             paste the six IDs into SELECTORS below.
  resolve    Match each COLLEGE_TOKENS entry to exactly one dropdown option
             and write the crosswalk to college_resolution.csv. Fails loudly
             on any token that is ambiguous or absent.
  pull       Download the CSVs. Safe to re-run.
  status     Print which cells are still missing.
  inspect    Print the first 20 raw lines of one exported CSV (do this
             before trusting `normalize`).
  normalize  Melt the exported pivot CSVs into one tidy long CSV.

Setup
-----
  pip install playwright
  playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

URL = "https://datamart.cccco.edu/outcomes/Program_Awards.aspx"
OUT = Path("./datamart_out")
RAW = OUT / "raw"
DISCOVERY = Path("./discovery")
MANIFEST = OUT / "manifest.csv"
TIDY = OUT / "program_awards_tidy.csv"

YEARS = [
    "Annual 2024-2025",
    "Annual 2023-2024",
    "Annual 2022-2023",
]

# Short, unambiguous tokens. `resolve` matches each one against the actual
# District-College dropdown options (case-insensitive substring) and refuses
# to run unless every token hits exactly one option. Filenames use the token;
# the resolved dropdown string is recorded in college_resolution.csv.
COLLEGE_TOKENS: list[str] = [
    "Madera", "Fresno", "Clovis", "Reedley",
    "Bakersfield", "Cerro Coso", "Porterville", "Sequoias", "Taft",
    "Lemoore", "Coalinga",
    "San Joaquin Delta", "Columbia", "Modesto", "Merced",
]

RESOLUTION = OUT / "college_resolution.csv"

# Fill from discovery/control_ids.txt after running `discover`.
# Every value is a DOM id, used as a #id CSS selector.
SELECTORS = {
    "scope_combo": "ASPxRoundPanel1_ASPxComboBoxSDC",              # "Select State-District-College"
    "college_combo": "ASPxRoundPanel1_ASPxDropDownEditDistColl",  # "Select District-College"
    "year_combo": "ASPxRoundPanel1_ASPxDropDownEditTerm",          # "Select Academic Year"
    "award_combo": "ASPxRoundPanel1_ASPxComboBoxAWType",           # "Select Award Type"
    "program_tree": "ASPxRoundPanel1_ASPxDropDownEditTOP",         # "Select Program Type" tree list
    "view_report_btn": "ASPxRoundPanel1_RunReportASPxButton",      # "View Report"
}

SCOPE_VALUE = "Collegewide Search"
AWARD_VALUE = "All Awards"
PROGRAM_ROOT_LABEL = "(All Programs)"

# Row fields to check in the "Report Format Selection Area". College Name is
# intentionally omitted: each pull is a single college, so it is a constant
# column and is carried by the filename instead.
ROW_FIELDS = ["Award Type", "Program Type - Six Digits TOP"]

NAV_TIMEOUT_MS = 120_000
CALLBACK_TIMEOUT_MS = 240_000   # Data Mart callbacks can genuinely take minutes
DOWNLOAD_TIMEOUT_MS = 300_000
MAX_ATTEMPTS = 1
BACKOFF_BASE_S = 20
HEADLESS = True

# ----------------------------------------------------------------------------
# waiting
# ----------------------------------------------------------------------------

_LOADING_JS = """
() => {
  const sel = '[id*="LoadingPanel"], [class*="dxlpLoadingPanel"], [class*="LoadingPanel"]';
  for (const el of document.querySelectorAll(sel)) {
    const s = window.getComputedStyle(el);
    if (s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null) {
      return false;
    }
  }
  return true;
}
"""


def wait_settled(page, timeout_ms: int = CALLBACK_TIMEOUT_MS) -> None:
    """Block until every DevExpress loading panel is hidden and the network
    is quiet. This replaces the fixed sleep that makes agentic loops thrash."""
    deadline = time.monotonic() + timeout_ms / 1000
    # Give the callback a moment to actually start before we test for quiet.
    page.wait_for_timeout(500)
    page.wait_for_function(_LOADING_JS, timeout=timeout_ms)
    remaining = max(1000, int((deadline - time.monotonic()) * 1000))
    try:
        page.wait_for_load_state("networkidle", timeout=remaining)
    except PWTimeout:
        # networkidle can be defeated by long-poll/keepalive; the loading
        # panel check above is the authoritative signal.
        pass


def dismiss_session_dialog(page) -> None:
    """Data Mart shows a 'Your session is about to expire!' modal. Click OK
    if it is up so it cannot swallow a later click."""
    try:
        ok = page.get_by_role("button", name=re.compile(r"^\s*OK\s*$"))
        if ok.count() and ok.first.is_visible():
            ok.first.click()
            wait_settled(page, 30_000)
    except PWError:
        pass


# ----------------------------------------------------------------------------
# DevExpress interaction helpers
# ----------------------------------------------------------------------------

def set_combo(page, control_id: str, value: str) -> None:
    """Open an ASPxComboBox by id and pick the item whose text equals `value`."""
    if not control_id:
        raise SystemExit("SELECTORS is not filled in. Run `discover` first.")
    dismiss_session_dialog(page)
    # The dropdown button is rendered as <id>_B-1 in every ASPx version I have
    # seen; fall back to clicking the container.
    btn = page.locator(f'#{control_id}_B-1, #{control_id}_B0, #{control_id} img')
    (btn.first if btn.count() else page.locator(f"#{control_id}")).click()
    wait_settled(page, 60_000)
    item = page.locator("td[class*='dxeListBoxItem'], li[class*='dxeListBoxItem']").filter(
        has_text=re.compile(rf"^\s*{re.escape(value)}\s*$")
    )
    item.first.click(timeout=60_000)
    wait_settled(page)


def select_all_programs(page, control_id: str) -> None:
    """Check the root '(All Programs)' node of the Program Type tree."""
    if not control_id:
        raise SystemExit("SELECTORS is not filled in. Run `discover` first.")
    dismiss_session_dialog(page)
    page.locator(f"#{control_id}").click()
    wait_settled(page, 60_000)
    # This is a DevExpress tree, not a table/listbox. The root checkbox is
    # rendered as the N0_D checkbox span; there is no ancestor <tr> to walk.
    # Use the discovered root node directly instead of an XPath union. The
    # prior union contained two `xpath=` prefixes and caused Playwright's
    # query engine to reject the expression as a non-node-set result.
    root_box = page.locator(
        f"#{control_id}_DDD_DDTC_ASPxCallbackPanel1_ASPxTreeView1_N0_D"
    )
    if not root_box.count():
        raise RuntimeError(
            f"All Programs checkbox not found for tree control {control_id!r}"
        )
    root_box.first.click(timeout=60_000, force=True)
    wait_settled(page)
    close = page.get_by_role("button", name=re.compile(r"^\s*Close\s*$"))
    if close.count() and close.first.is_visible():
        close.first.click()
        wait_settled(page, 60_000)


def check_row_fields(page) -> None:
    """Tick the Row Options boxes, then click Update Report."""
    for label in ROW_FIELDS:
        dismiss_session_dialog(page)
        node = page.get_by_text(label, exact=True).first
        box = node.locator("xpath=preceding::input[@type='checkbox'][1]")
        target = box.first if box.count() else node
        try:
            if box.count() and box.first.is_checked():
                continue
        except PWError:
            pass
        target.click(timeout=60_000)
        wait_settled(page)
    # DevExpress exposes a visible DIV button plus a hidden submit INPUT.
    # get_by_text() may resolve the hidden INPUT, which Playwright cannot
    # click. Target the visible outer button explicitly.
    page.locator("#ASPxRoundPanel3_UpdateReport").click(timeout=60_000)
    wait_settled(page)


def export_csv(page, dest: Path) -> None:
    """Select the CSV radio, click Export To, capture the download."""
    dismiss_session_dialog(page)
    csv_radio = page.get_by_text("CSV", exact=True).first
    csv_radio.click()
    wait_settled(page, 60_000)
    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl:
        # As with Update Report, click the visible DevExpress button wrapper,
        # not its hidden *_I submit input.
        page.locator("#buttonSaveAs").click(timeout=DOWNLOAD_TIMEOUT_MS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dl.value.save_as(str(dest))


# ----------------------------------------------------------------------------
# discover
# ----------------------------------------------------------------------------

DISCOVER_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('[id]')) {
    const id = el.id;
    if (!/Combo|cbo|ddl|Tree|Check|Btn|Button|Export|Report|Year|College|District|Award|Program/i.test(id)) continue;
    const txt = (el.innerText || el.value || '').trim().slice(0, 80).replace(/\\s+/g, ' ');
    out.push({id, tag: el.tagName, cls: (el.className || '').toString().slice(0, 60), txt});
  }
  return out;
}
"""


def cmd_discover(_args) -> None:
    DISCOVERY.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(accept_downloads=True)
        page.set_default_timeout(NAV_TIMEOUT_MS)
        page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        wait_settled(page)

        rows = page.evaluate(DISCOVER_JS)
        with (DISCOVERY / "control_ids.txt").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(f"{r['id']}\t{r['tag']}\t{r['cls']}\t{r['txt']}\n")

        # District-College options only populate after scope = Collegewide.
        try:
            set_combo(page, SELECTORS["scope_combo"], SCOPE_VALUE)
        except SystemExit:
            print("scope_combo not set yet — open discovery/control_ids.txt, "
                  "fill SELECTORS, then run `discover` again to dump the "
                  "college list.", file=sys.stderr)
            browser.close()
            return

        opts = read_college_options(page)
        (DISCOVERY / "district_college_options.txt").write_text(
            "\n".join(opts), encoding="utf-8"
        )
        print(f"wrote {DISCOVERY}/control_ids.txt and "
              f"{DISCOVERY}/district_college_options.txt")
        browser.close()


# ----------------------------------------------------------------------------
# pull
# ----------------------------------------------------------------------------

def read_college_options(page) -> list[str]:
    """Open the District-College combo and return every option string."""
    btn = page.locator(f"#{SELECTORS['college_combo']}_B-1, "
                       f"#{SELECTORS['college_combo']}_B0")
    (btn.first if btn.count() else
     page.locator(f"#{SELECTORS['college_combo']}")).click()
    wait_settled(page)
    opts = [o.strip() for o in
            page.locator("td[class*='dxeListBoxItem']").all_inner_texts()]
    return [o for o in opts if o]


def resolve_tokens(options: list[str]) -> dict[str, str]:
    """Map each token to exactly one dropdown option. Raises on 0 or >1 so the
    run fails at cell zero rather than 12 cells in."""
    resolved, problems = {}, []
    for tok in COLLEGE_TOKENS:
        hits = [o for o in options if tok.lower() in o.lower()]
        if len(hits) == 1:
            resolved[tok] = hits[0]
        else:
            problems.append((tok, hits))
    if problems:
        for tok, hits in problems:
            if hits:
                print(f"AMBIGUOUS  {tok!r} -> {len(hits)} matches:", file=sys.stderr)
                for h in hits:
                    print(f"             {h}", file=sys.stderr)
            else:
                print(f"NO MATCH   {tok!r}", file=sys.stderr)
        raise SystemExit("fix COLLEGE_TOKENS (use a longer, unique fragment) "
                         "and re-run `resolve`.")
    return resolved


def load_resolution() -> dict[str, str]:
    if not RESOLUTION.exists():
        raise SystemExit("no college_resolution.csv — run `resolve` first.")
    with RESOLUTION.open(encoding="utf-8") as fh:
        m = {r["token"]: r["dropdown_option"] for r in csv.DictReader(fh)}
    missing = [t for t in COLLEGE_TOKENS if t not in m]
    if missing:
        raise SystemExit(f"resolution file is stale, missing {missing}. "
                         "Re-run `resolve`.")
    return m


def cmd_resolve(_args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(accept_downloads=True)
        page.set_default_timeout(NAV_TIMEOUT_MS)
        page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        wait_settled(page)
        set_combo(page, SELECTORS["scope_combo"], SCOPE_VALUE)
        options = read_college_options(page)
        browser.close()
    resolved = resolve_tokens(options)
    with RESOLUTION.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["token", "dropdown_option"])
        for tok in COLLEGE_TOKENS:
            w.writerow([tok, resolved[tok]])
            print(f"{tok:20s} -> {resolved[tok]}")
    print(f"\n{len(resolved)} colleges resolved, written to {RESOLUTION}")


def cell_path(token: str, year: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", token).strip("_")
    yr = year.replace("Annual ", "").replace("-", "_")
    return RAW / f"{slug}__{yr}.csv"


def cells() -> list[tuple[str, str, Path]]:
    return [(t, y, cell_path(t, y)) for t in COLLEGE_TOKENS for y in YEARS]


def is_done(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 200


def pull_one(browser, college: str, year: str, dest: Path) -> None:
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        wait_settled(page)
        set_combo(page, SELECTORS["scope_combo"], SCOPE_VALUE)
        set_combo(page, SELECTORS["college_combo"], college)
        set_combo(page, SELECTORS["year_combo"], year)
        set_combo(page, SELECTORS["award_combo"], AWARD_VALUE)
        select_all_programs(page, SELECTORS["program_tree"])
        page.locator(f"#{SELECTORS['view_report_btn']}").click()
        wait_settled(page)
        check_row_fields(page)
        export_csv(page, dest)
    finally:
        ctx.close()


def cmd_pull(args) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    resolution = load_resolution()
    todo = [(t, y, p) for t, y, p in cells() if not is_done(p)]
    print(f"{len(todo)} cells to pull ({len(cells()) - len(todo)} already on disk)")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        for i, (token, year, dest) in enumerate(todo, 1):
            college = resolution[token]
            status, err, attempts = "failed", "", 0
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt
                t0 = time.monotonic()
                try:
                    pull_one(browser, college, year, dest)
                    if not is_done(dest):
                        raise RuntimeError("export produced an empty file")
                    status = "ok"
                    print(f"[{i}/{len(todo)}] {college} {year} "
                          f"({time.monotonic() - t0:.0f}s, attempt {attempt})")
                    break
                except Exception as exc:  # noqa: BLE001 - log and retry
                    err = f"{type(exc).__name__}: {exc}"[:300]
                    print(f"[{i}/{len(todo)}] {college} {year} attempt "
                          f"{attempt} failed: {err}", file=sys.stderr)
                    dest.unlink(missing_ok=True)
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(BACKOFF_BASE_S * attempt)
            digest = (hashlib.sha256(dest.read_bytes()).hexdigest()
                      if is_done(dest) else "")
            rows.append({
                "token": token, "college": college, "year": year,
                "file": str(dest), "status": status, "attempts": attempts,
                "sha256": digest, "error": err,
            })
        browser.close()

    write_header = not MANIFEST.exists()
    with MANIFEST.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                           ["token", "college", "year", "file", "status",
                            "attempts", "sha256", "error"])
        if write_header:
            w.writeheader()
        w.writerows(rows)
    bad = [r for r in rows if r["status"] != "ok"]
    print(f"done. {len(rows) - len(bad)} ok, {len(bad)} failed. "
          f"Re-run `pull` to retry the failures.")


def cmd_status(_args) -> None:
    missing = [(c, y) for c, y, p in cells() if not is_done(p)]
    total = len(cells())
    print(f"{total - len(missing)}/{total} cells present")
    for c, y in missing:
        print(f"MISSING\t{c}\t{y}")
    sys.exit(1 if missing else 0)


# ----------------------------------------------------------------------------
# inspect / normalize
# ----------------------------------------------------------------------------

def cmd_inspect(_args) -> None:
    files = sorted(RAW.glob("*.csv"))
    if not files:
        raise SystemExit("no exports in ./datamart_out/raw yet")
    print(f"--- {files[0]} ---")
    for line in files[0].read_text(encoding="utf-8-sig",
                                   errors="replace").splitlines()[:20]:
        print(repr(line))


def cmd_normalize(_args) -> None:
    """Melt the pivot exports into one tidy long CSV.

    Confirm the layout with `inspect` before trusting this. The pivot export
    nests row fields and leaves parent labels blank on continuation rows, so
    the parser forward-fills them.
    """
    files = sorted(RAW.glob("*.csv"))
    if not files:
        raise SystemExit("no exports in ./datamart_out/raw yet")
    OUT.mkdir(parents=True, exist_ok=True)
    top6 = re.compile(r"^(?P<name>.*?)[-\u2013](?P<code>\d{6})$")

    try:
        resolution = load_resolution()
    except SystemExit:
        resolution = {}

    with TIDY.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["college", "academic_year", "award_type",
                    "top6_code", "top6_name", "awards", "source_file"])
        for path in files:
            slug, _, yr = path.stem.partition("__")
            year = yr.replace("_", "-")
            # cell_path() slugs tokens with re.sub(r"[^A-Za-z0-9]+", "_"), so
            # "San Joaquin Delta" -> "San_Joaquin_Delta" and
            # "Cerro Coso"       -> "Cerro_Coso".  Resolve the token by
            # slugifying each resolution key and matching exactly, instead of
            # blanket-restoring underscores to spaces (Cerro_Coso ->
            # "Cerro_Coso" never matched token "Cerro Coso", so the raw slug
            # leaked into the college column).
            token = next(
                (t for t in resolution if re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_") == slug),
                slug.replace("_", " "),
            )
            college = resolution.get(token, token)
            award = ""
            reader = csv.reader(path.read_text(encoding="utf-8-sig",
                                               errors="replace").splitlines())
            for row in reader:
                cells_ = [c.strip() for c in row]
                if not any(cells_):
                    continue
                labels = [c for c in cells_ if c and not re.fullmatch(r"[\d,]+", c)]
                nums = [c for c in cells_ if re.fullmatch(r"[\d,]+", c)]
                if not labels:
                    continue
                label = labels[-1]
                m = top6.match(label)
                if m and nums:
                    w.writerow([college, year, award, m.group("code"),
                                m.group("name").strip(), nums[0].replace(",", ""),
                                path.name])
                elif not m and "Total" not in label:
                    award = label
    print(f"wrote {TIDY}")


# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("discover", cmd_discover), ("resolve", cmd_resolve),
                     ("pull", cmd_pull), ("status", cmd_status),
                     ("inspect", cmd_inspect), ("normalize", cmd_normalize)]:
        sub.add_parser(name).set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

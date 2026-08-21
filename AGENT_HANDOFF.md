# AGENT_HANDOFF.md — running `datamart_awards.py`

You are operating a scraper for the CCCCO Data Mart Program Awards Summary
report. Read this whole file before running anything.

---

## The one rule that matters

**You do not drive the browser. The script does.**

Data Mart is a DevExpress ASPxPivotGrid application whose parameter changes
are server-side callbacks. A single callback can take two to four minutes to
return. The script already handles every wait, retry, backoff, and resume.

Your job is exactly three things:

1. Run a subcommand.
2. Read its output.
3. Decide whether to run another one.

If you find yourself writing browser automation, adding `sleep`, polling a
page, or wrapping `pull` in a shell loop, stop — you are re-introducing the
failure this script exists to eliminate.

---

## Setup

```bash
pip install playwright
playwright install chromium
```

Everything is relative to the script's working directory. It creates
`./discovery/` and `./datamart_out/`.

---

## Run order

Six subcommands, in this order. Do not skip ahead.

| # | Command | Runtime | What it does |
|---|---------|---------|--------------|
| 1 | `python datamart_awards.py discover` | ~2 min | Dumps DevExpress control IDs and dropdown options |
| 2 | *(edit `SELECTORS`)* | manual | See "Step 2" below — this is the only editing you do |
| 3 | `python datamart_awards.py resolve` | ~2 min | Maps the 15 college tokens to exact dropdown strings |
| 4 | `python datamart_awards.py pull` | **30–90 min** | Downloads 45 CSVs, one per college × year |
| 5 | `python datamart_awards.py status` | instant | Lists any missing cells; exit code 1 if incomplete |
| 6 | `python datamart_awards.py inspect` then `normalize` | instant | Melts the pivot exports into one tidy CSV |

---

## Step 2 — filling in `SELECTORS`

This is the only part requiring judgment. After `discover`, read
`discovery/control_ids.txt`. It is tab-separated: `id`, `tag`, `class`, `text`.

Find the DOM id for each of these six controls and paste it into the
`SELECTORS` dict at the top of `datamart_awards.py`:

| Key | Page label | Look for |
|-----|-----------|----------|
| `scope_combo` | Select State-District-College | id containing `State` or `Scope`, class `dxeEditor` / `dxeButtonEdit` |
| `college_combo` | Select District-College | id containing `District` or `College` |
| `year_combo` | Select Academic Year | id containing `Year` or `Term` |
| `award_combo` | Select Award Type | id containing `Award` |
| `program_tree` | Select Program Type | id containing `Program` or `Tree`; this is a tree list, not a combo |
| `view_report_btn` | View Report | id containing `View` or `Report`, tag `INPUT` or `DIV` with button classes |

Rules:

- Use the id of the **outer editor container**, not the inner `_I` text input
  or `_B-1` dropdown button. The script derives those itself.
- Take ids verbatim. They are long and ASP.NET-generated (`ctl00_...`) and a
  single wrong character fails all 45 cells identically.
- If two candidates look plausible, pick one and run `resolve`. If `resolve`
  returns the college list, the ids are right. That is the cheap test — do
  not verify by running `pull`.

---

## Step 3 — `resolve`

Prints a token-to-dropdown-option crosswalk and writes
`datamart_out/college_resolution.csv`.

- **All 15 resolve** → proceed to `pull`.
- **`NO MATCH`** → the token does not appear in the dropdown. Report the token
  and the surrounding options to the user. Do not substitute a guess.
- **`AMBIGUOUS`** → the token hit more than one option. Report the candidates
  and ask which one. Do not pick.

Do not edit `college_resolution.csv` by hand. Regenerate it.

---

## Step 4 — `pull`

Expect this to run for **30 to 90 minutes** with long silences between lines.
That is normal. Data Mart is slow.

Behavior you must not misread as failure:

- **No output for several minutes.** A callback is in flight. Wait.
- **A cell retried two or three times.** The script backs off 20s, 40s, 60s
  and retries up to 4 times. This is designed behavior, not an error state.
- **Individual cells failing while others succeed.** Normal. `pull` finishes
  the run, logs failures to `manifest.csv`, and you re-run.

Things you must not do:

- Do not run `pull` in a `while` loop or a `watch`.
- Do not run two `pull` processes at once. Data Mart's session state is
  per-session and concurrency causes session expiry and silently truncated
  exports.
- Do not lower `CALLBACK_TIMEOUT_MS`, `DOWNLOAD_TIMEOUT_MS`, or
  `BACKOFF_BASE_S`. These are tuned to the server's real latency.
- Do not set `HEADLESS = False` unless you are debugging selectors
  interactively with a human watching.
- Do not delete `datamart_out/raw/` to "start clean." Those files are the
  checkpoint.

`pull` is idempotent: a non-empty output file is skipped. Kill it, crash it,
lose the session — re-running resumes from where it stopped.

---

## Step 5 — `status`

```bash
python datamart_awards.py status
```

Prints `N/45 cells present` and one `MISSING` line per gap. Exit code 1 if
anything is missing, 0 if complete.

Decision rule:

- **Missing cells, and the last `pull` made progress** → run `pull` again.
- **Missing cells, and the last `pull` made zero progress** → stop. Do not
  re-run a third time. Read the `error` column of `manifest.csv` and report
  it to the user. Repeated zero-progress runs mean a selector broke or Data
  Mart is down, and neither is fixed by retrying.
- **Exit 0** → proceed to `inspect`.

Two `pull` runs is the ceiling before you escalate to the user.

---

## Step 6 — `inspect`, then `normalize`

```bash
python datamart_awards.py inspect
```

Prints the first 20 raw lines of one export. **Read them.** The pivot export
nests row labels and blanks the parent on continuation rows, and the parser in
`normalize` assumes a specific shape:

- an award-type label line with no six-digit code,
- followed by lines ending in a six-digit TOP code plus a count.

If `inspect` shows something different — a different delimiter, a quoted
multi-line header, codes formatted with a period (`0104.00` rather than
`010400`) — **do not run `normalize`**. Report the actual layout to the user
and let them adjust the parser. A silently mis-melted CSV is worse than none.

If the layout matches, run:

```bash
python datamart_awards.py normalize
```

Output: `datamart_out/program_awards_tidy.csv` with columns
`college, academic_year, award_type, top6_code, top6_name, awards, source_file`.

---

## Outputs

```
discovery/
  control_ids.txt                 DevExpress ids (step 1)
  district_college_options.txt    every District-College dropdown option
datamart_out/
  college_resolution.csv          token -> dropdown string crosswalk
  raw/<Token>__<YYYY_YYYY>.csv    45 pivot exports, one per cell
  manifest.csv                    append-only log: status, attempts, sha256, error
  program_awards_tidy.csv         final long-format output
```

`manifest.csv` is append-only and carries a sha256 per file. It is the audit
trail — do not truncate or rewrite it.

---

## Scope (do not change without being asked)

- 15 colleges: Madera, Fresno, Clovis, Reedley, Bakersfield, Cerro Coso,
  Porterville, Sequoias, Taft, Lemoore, Coalinga, San Joaquin Delta,
  Columbia, Modesto, Merced
- 3 years: Annual 2024-2025, 2023-2024, 2022-2023
- Award Type: All Awards
- Program Type: all programs
- Row Options: Award Type, Program Type - Six Digits TOP
  (College Name is deliberately excluded — one college per pull, carried by
  the filename)

---

## Failure triage

| Symptom | Cause | Action |
|---|---|---|
| `SELECTORS is not filled in` | Step 2 not done | Do step 2 |
| `no college_resolution.csv` | `resolve` not run | Run `resolve` |
| Every cell fails identically, fast | Wrong id in `SELECTORS` | Re-read `control_ids.txt`; fix the id |
| Every cell fails identically, slow (timeout) | Data Mart down or blocking | Stop. Report to user. Retry later |
| Some cells fail, some succeed | Normal server flakiness | Re-run `pull` once |
| Downloads land empty | Row Options never applied, or session expired mid-export | Report to user with the `error` column |
| `resolve` returns AMBIGUOUS | Token matches multiple options | Ask the user which; do not guess |

When you escalate, include: the subcommand, the `error` column from
`manifest.csv`, and the `status` count. Do not paste the full manifest.

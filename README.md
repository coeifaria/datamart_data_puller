# CCCCO Datamart Data Puller

This application pulls **program success-outcome data** from the California Community Colleges Chancellor's Office (CCCCO) Data Mart for 15 Central Valley and surrounding community colleges.

The scraper uses Playwright to operate the Data Mart's stateful DevExpress report interface and exports one CSV per college and academic year. The downloaded records support analysis of awards and program outcomes by six-digit TOP code.

## Colleges

The configured 15 colleges are:

- Madera
- Fresno City
- Clovis
- Reedley College
- Bakersfield
- Cerro Coso
- Porterville
- Sequoias
- Taft
- Lemoore College
- Coalinga College
- San Joaquin Delta
- Columbia
- Modesto
- Merced

## Current extraction scope

- **Academic years:** 2024–2025, 2023–2024, and 2022–2023
- **Award type:** All Awards
- **Program type:** All Programs
- **Report rows:** Award Type and Program Type – Six Digits TOP
- **Expected output:** 15 colleges × 3 years = 45 CSV exports

The 2025–2026 term is intentionally excluded because the Data Mart identifies the most recent term as incomplete until all districts submit their data.

## Requirements

- Python 3.11+
- Playwright
- Chromium installed for Playwright

Install the dependency and browser:

```bash
python -m pip install playwright
python -m playwright install chromium
```

## Usage

Run commands from the repository directory.

### 1. Discover controls and dropdown values

```bash
python datamart_awards.py discover
```

Discovery writes the live DevExpress control IDs and dropdown options to `discovery/`.

### 2. Resolve the college names

```bash
python datamart_awards.py resolve
```

This maps the configured short tokens to the exact college names used by the Data Mart and writes `datamart_out/college_resolution.csv`.

### 3. Pull the exports

```bash
python datamart_awards.py pull
```

The pull is sequential and resumable. Existing non-empty files are skipped. Outputs are written to:

```text
datamart_out/raw/<CollegeToken>__<YYYY_YYYY>.csv
```

Raw downloaded CSV files are intentionally excluded from Git tracking by `.gitignore`.

### 4. Check progress

```bash
python datamart_awards.py status
```

This reports how many of the expected 45 college/year cells are present.

### 5. Inspect and normalize

Inspect one raw export before normalization:

```bash
python datamart_awards.py inspect
```

If the layout matches the parser's expected pivot format, create a tidy combined file:

```bash
python datamart_awards.py normalize
```

The normalized output is `datamart_out/program_awards_tidy.csv` with columns:

```text
college, academic_year, award_type, top6_code, top6_name, awards, source_file
```

## Repository files

- `datamart_awards.py` — scraper and normalization commands
- `AGENT_HANDOFF.md` — operational runbook and failure-triage guidance
- `discovery/` — discovered Data Mart controls and dropdown options
- `datamart_out/college_resolution.csv` — token-to-dropdown-name mapping
- `datamart_out/raw/` — local downloaded exports; not tracked by Git

## Data source

California Community Colleges Chancellor's Office Data Mart:

<https://datamart.cccco.edu/outcomes/Program_Awards.aspx>

## Operational notes

The Data Mart uses server-side callbacks and can be slow. The scraper handles the browser waits and is designed to run one cell at a time. Do not run multiple `pull` processes concurrently, and do not wrap `pull` in an external retry loop.

The exported data should be validated with `inspect` before relying on `normalize` for analysis.

## License

No license has been selected for this repository yet.
# end

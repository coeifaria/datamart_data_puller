#!/usr/bin/env Rscript
# transform_datavista.R  (v2)
#
# Rebuilds the legacy 20-column CVML extract shape from the new DataVista
# pipe-delimited regional exports, then joins the authoritative CTE flag from
# TOP_CTE_082726.csv and filters to the Submetric Menu Name items used by the
# CVML success-metrics workbooks.
#
#   Inputs:
#     Central_Mother_Lode_2023/2024/2025.csv  (pipe-delimited, 15 cols, one per year)
#     TOP_CTE_082726.csv                      (Current TOP Code like 0102.10 -> CTE / Not CTE)
#
#   Output: DataVista_CVML_2023_2025.csv (legacy 20-column shape, UTF-8 BOM, CRLF)
#
#   Legacy-shape conventions preserved:
#     - ValueOriginal integer-formatted (new VALUE is decimal; counts are whole)
#     - missing values are empty strings (never the literal string 'None')
#     - Perc (copy) / ValueOriginal (copy) / VO2 mirror their source columns
#     - CTE1 + Current TOP Code populated only for numeric TOP-code rows
#
#   v2 changes:
#     - CTE flag now comes from TOP_CTE_082726.csv (authoritative), not the old
#       extract's self-derived lookup. Codes absent from the taxonomy file get
#       a blank flag (matches legacy behavior for unknown codes).
#     - Submetric Menu Name filter applied (11 items incl. Students/Course
#       Success Rate); rows whose Submetric is 'None' are kept, matching the
#       legacy extract, which also carried blank Submetrics.

suppressPackageStartupMessages(library(data.table))

src_dir   <- "C:/Users/if001/Documents/CoE/Central_Mother_Lode/Central_Mother_Lode"
src_files <- c(
  file.path(src_dir, "Central_Mother_Lode_2023.csv"),
  file.path(src_dir, "Central_Mother_Lode_2024.csv"),
  file.path(src_dir, "Central_Mother_Lode_2025.csv")
)
top_cte_path <- "TOP_CTE_082726.csv"
out_path <- "DataVista_CVML_2023_2025.csv"

metrics_keep <- c("Course Success Rate", "Earned an Award", "Students")
ptypes_keep  <- c("Top 4", "Top 6", "Sector", "All Programs")  # legacy universe; excludes new 'CTE' rollup
submetrics_keep <- c(
  "Apprenticeship Journey Level Status",
  "Associate Degree",
  "Associate Degree (Not for Transfer)",
  "Associate Degree for Transfer",
  "Chancellor's Office Approved Credit Certificate",
  "Community College Bachelor's Degree",
  "Course Success Rate",
  "Degree or Certificate or Attained Apprenticeship Journey Level Status",
  "Noncredit Certificate",
  "Students",
  "Vision Goal Completion Definition"
)

none_to_blank <- function(x) {
  x[x == "None"] <- ""
  x
}

## 1. CTE taxonomy lookup -------------------------------------------------------
# Current TOP Code is dot-formatted (0102.10); dataset Program codes are plain
# 6-digit (010210). Normalize by stripping the dot.
message("Building CTE lookup from TOP_CTE_082726.csv ...")
lut <- fread(top_cte_path, colClasses = list(character = c("Current TOP Code", "CTE")))
setnames(lut, "Current TOP Code", "Current_TOP_Code")
stopifnot(all(lut$CTE %in% c("CTE", "Not CTE")))
lut[, TOP6 := sub("\\.", "", Current_TOP_Code)]
lut <- unique(lut[, .(TOP6, CTE)])
conflicts <- lut[, .N, by = TOP6][N > 1]
stopifnot(nrow(conflicts) == 0)
cte_lut <- lut[, .(Program = TOP6, CTE1 = CTE)]
setkey(cte_lut, Program)
message(sprintf("  %d TOP codes in CTE lookup", nrow(cte_lut)))
rm(lut, conflicts)

## 2. Stream the three new files ----------------------------------------------
col_sel <- c(
  "METRIC_THEME", "NEW_METRIC_ID", "METRIC_MENU_NAME", "SUBMETRIC_MENU_NAME",
  "LOCALE_TYPE", "LOCALE_NAME", "PROGRAM_TYPE", "PROGRAM", "PROGRAM_MENU_NAME",
  "DISAGG1_LABEL", "SUBGROUP1_LABEL", "YEAR_ID", "VALUE", "DENOM", "PERC"
)

parts <- lapply(src_files, function(f) {
  message(sprintf("Reading %s ...", basename(f)))
  dt <- fread(f, sep = "|", select = col_sel, encoding = "UTF-8")
  n0 <- nrow(dt)
  dt <- dt[METRIC_MENU_NAME %in% metrics_keep & PROGRAM_TYPE %in% ptypes_keep]
  # Submetric filter: keep the 11 workbook items; also keep blank/'None'
  # submetrics (legacy extract carried them).
  dt[, SUB_clean := none_to_blank(SUBMETRIC_MENU_NAME)]
  dt <- dt[SUB_clean == "" | SUB_clean %in% submetrics_keep]
  dt[, SUB_clean := NULL]
  message(sprintf("  %d -> %d rows after metric/program-type/submetric filter", n0, nrow(dt)))
  dt[, METRIC_THEME := NULL]
  dt
})
dat <- rbindlist(parts)
rm(parts)

## 3. Value formatting to legacy conventions ----------------------------------
dat[, DENOM := none_to_blank(DENOM)]
dat[, PERC  := none_to_blank(PERC)]
dat[, SUBMETRIC_MENU_NAME := none_to_blank(SUBMETRIC_MENU_NAME)]

dat[, VALUE_num := suppressWarnings(as.numeric(VALUE))]
dat[, VALUE_int := fifelse(is.na(VALUE_num), "",
                           as.character(as.integer(round(VALUE_num, 0))))]
dat[, c("VALUE", "VALUE_num") := NULL]

## 4. Derive legacy columns ----------------------------------------------------
# CTE flag: join taxonomy on the numeric Program code. Unmatched -> blank
# (legacy file also carried unflagged numeric rows for unknown codes).
# NOTE: bare PROGRAM only resolves inside DT[...] scope — compute is_num
# with dat$PROGRAM explicitly.
lut_vec <- setNames(cte_lut$CTE1, cte_lut$Program)
is_num <- grepl("^[0-9]+$", dat$PROGRAM)
flags <- lut_vec[dat$PROGRAM[is_num]]                     # NA when code not in taxonomy
flags <- unname(ifelse(is.na(flags), "", flags))
dat[is_num, `Current TOP Code` := as.character(dat$PROGRAM[is_num])]
dat[is_num, CTE1 := flags]

dat[, primary_key := paste0(
  YEAR_ID, " ", DISAGG1_LABEL, " | ", LOCALE_NAME, " | ", LOCALE_TYPE, " | ",
  METRIC_MENU_NAME, " | ", NEW_METRIC_ID, " | ", PROGRAM, " | ",
  PROGRAM_MENU_NAME, " | ", PROGRAM_TYPE, " | ", SUBGROUP1_LABEL, " | ",
  SUBMETRIC_MENU_NAME
)]

setnames(dat,
         c("DISAGG1_LABEL", "LOCALE_NAME", "LOCALE_TYPE", "METRIC_MENU_NAME",
           "NEW_METRIC_ID", "PERC", "DENOM", "PROGRAM", "PROGRAM_MENU_NAME",
           "PROGRAM_TYPE", "SUBGROUP1_LABEL", "VALUE_int", "SUBMETRIC_MENU_NAME",
           "YEAR_ID"),
         c("Disagg1 Label", "Locale Name", "Locale Type", "Metric Menu Name",
           "New Metric Id", "Perc", "Denom", "Program", "Program Menu Name",
           "Program Type", "Subgroup", "ValueOriginal", "Submetric Menu Name",
           "Year Id"))

dat[, `Perc (copy)`          := Perc]
dat[, `ValueOriginal (copy)` := ValueOriginal]
dat[, VO2                    := ValueOriginal]

final_cols <- c(
  "CTE1", "Current TOP Code", "Denom", "Disagg1 Label", "Locale Name",
  "Locale Type", "Metric Menu Name", "New Metric Id", "Perc", "Perc (copy)",
  "primary_key", "Program", "Program Menu Name", "Program Type", "Subgroup",
  "Submetric Menu Name", "ValueOriginal", "Year Id", "ValueOriginal (copy)", "VO2"
)
setcolorder(dat, final_cols)

# Deterministic order close to the legacy extract (year, metric, locale,
# program, subgroup).
setorder(dat, `Year Id`, `Metric Menu Name`, `Locale Name`, Program, Subgroup)

## 5. Write --------------------------------------------------------------------
message(sprintf("Writing %s (%d rows) ...", out_path, nrow(dat)))
fwrite(dat, out_path, sep = ",", eol = "\r\n", bom = TRUE, quote = TRUE)

message("Done. Rows per year:")
print(dat[, .N, by = `Year Id`][order(`Year Id`)])
message("Rows per metric:")
print(dat[, .N, by = `Metric Menu Name`][order(-N)])
message("CTE1 distribution among numeric-TOP rows:")
print(dat[CTE1 != "", .N, by = CTE1])
message(sprintf("Numeric-TOP rows with no taxonomy match (blank CTE1): %d",
                dat[CTE1 == "" & grepl("^[0-9]+$", Program), .N]))

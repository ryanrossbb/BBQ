---
name: quote-spreadsheeter
description: Use this skill whenever the user wants carrier insurance quotes compiled into BrokersBloc's medical comparison spreadsheet. Triggers include phrases like "spreadsheet these quotes," "build the quote comparison," "put these quotes in the template," "compare these plans," or any time the user uploads a mix of carrier quote files (PDF or Excel) plus a census document and asks for a summary — even if they don't say "spreadsheet" explicitly. Especially trigger when the user names specific plans to include (e.g., "spreadsheet these quotes for the HMO Gold, PPO Silver, and HDHP plans"). Use this skill for medical plan comparisons; do not use for dental, vision, or ancillary lines unless explicitly told to adapt the template.
---

# Quote Spreadsheeter

Compiles carrier medical quotes into the BrokersBloc Medical Comparison template. The user provides a list of plans to include, attaches the carrier quotes (PDF or Excel) and a census document, and gets back a populated `.xlsx` ready for client review.

## Inputs

Each run, the skill needs three things:

1. **A list of requested plans** — comes from the user's message, usually phrased as "spreadsheet these quotes for [plan A], [plan B], [plan C]." If not provided, ask before starting.
2. **Carrier quote files** — PDFs or Excel files, one or more per carrier. May contain multiple plans per file.
3. **A census document** — either an Excel file with employee-level rows (one row per enrollee), or a PDF with a roster table. Used to count enrollment by tier.

## Identifying which file is which

The user uploads a mix of files without labeling them. Identify each file by:

- **Census**: filename contains "census," "roster," or "enrollment." If no filename match, the census is the file that has one row per employee with a coverage tier indicator (see "Counting enrollment" below). If still ambiguous, ask the user.
- **Carrier quotes**: everything else. Carrier name usually appears in the filename or on the first page of the document.
- **Current/incumbent carrier**: identify in this order:
  1. User says it explicitly in the message ("current is Gravie", "renewal from Level Health")
  2. A filename contains "current," "renewal," or "incumbent"
  3. If neither, ask the user before proceeding. Do not guess — the entire cost analysis section depends on getting this right.

## Counting enrollment from the census

The template needs four numbers in column D (rows 34–37):
- D34: EE Only count
- D35: Employee + Spouse count
- D36: Employee + Child(ren) count
- D37: Employee + Family count

Count by tallying rows in the census, grouped by tier. Look for a column that indicates coverage tier — common labels and their mappings:

| Census value | Maps to |
|---|---|
| EE, EO, "Employee Only", "Employee", "Single" | EE Only |
| ES, "E+1" (with spouse), "Employee + Spouse", "EE+SP" | Employee + Spouse |
| EC, "E+1" (with child), "Employee + Child", "EE+CH", "Employee + Child(ren)" | Employee + Child(ren) |
| EF, "E+F", "Family", "Employee + Family", "EE+FAM" | Employee + Family |

If "E+1" is used ambiguously (could mean spouse OR child), check for a separate dependent type column. If you can't disambiguate, ask the user.

If the census uses a different scheme entirely, surface what you found and ask for the mapping before populating.

**Verify totals**: after counting, the sum of all four buckets should equal the total enrollee count. If it doesn't, flag it before proceeding.

## Matching requested plans to carrier quotes

**Plan name matching is EXACT.** If the user requests "HMO Gold" and a carrier quote says "Gold HMO 2000" or "Gold HMO Plan," that is **not** an exact match. In that case:

- Do not assume they're the same.
- Surface the candidates: "You asked for HMO Gold. The closest plans I found are: Gold HMO 2000 (Carrier X), Gold HMO Plan (Carrier Y). Should I use either of these?"
- Wait for confirmation before populating.

**If a requested plan isn't in any carrier quote**, flag the gap explicitly: "You asked for HDHP, but none of the uploaded carriers quoted an HDHP plan. Should I leave that column blank, drop it, or do you have an additional quote file?"

Do not silently omit, substitute, or merge plans.

## Populating the template

Use a copy of `assets/Prost11_Medical_Comparison.xlsx` (the BrokersBloc template). Save the populated copy as `[ClientName]_Medical_Comparison_[YYYY-MM-DD].xlsx`.

**Carrier column assignment:**
- Column F is always **CURRENT** (the incumbent carrier identified above).
- Columns G, H, I, J, K are alternate carriers, assigned in the order the user listed them (or alphabetically by carrier name if no order was specified).
- Maximum 6 carriers (F through K). If more were requested, ask the user which to drop or whether to use additional sheets.

**Header rows:**
- Row 9: carrier name (e.g., "Gravie")
- Row 10: plan name as it appears in the carrier's quote (e.g., "Gravie Copay $5,000 Ded/$7,900 OOPM")

**Benefits rows (12–32):** populate from the carrier quote. If the quote doesn't list a particular benefit (e.g., no Telehealth row), leave the cell blank — do not write "N/A" or invent values. Use the exact phrasing from the quote ("$30 copay", "20% coinsurance after deductible", "$5,000 / $10,000").

**Enrollment column (D):** drop in the four counts from the census.

**Monthly rate rows (34–37):** drop in the per-tier monthly rates from each carrier quote. These are dollars (e.g., `904.84`), not strings.

**Do not touch:**
- Rows 43–46 (Cost Analysis section) — these are pre-built formulas. They'll calculate automatically once rates and lives are populated. Leave them alone.
- The header block (rows 1–8), the section labels, the footer (rows 48–52).

**Update Group Name** in cell C3 if the user provides a client/group name in the request.

## Self-check before returning

Before saving, verify:

- Every requested plan got a column (or is explicitly flagged as a gap).
- Every populated rate cell is a number, not a string.
- The four enrollment counts sum to the total census.
- No `#REF!`, `#DIV/0!`, or other formula errors in rows 43–46. Run `python scripts/recalc.py [output_file]` to recalculate and check.
- The CURRENT column (F) has data — if it's blank, the cost analysis won't compute meaningfully.

## What to flag in the chat reply

Along with the saved file, list anything that needed judgment or has a gap:

- Plans you couldn't match exactly and how you resolved them
- Carriers that didn't quote a requested plan
- Census tier values that needed mapping
- Benefits rows that were blank in the source quote
- Any field where you had to ask the user during the run

Keep this list short and scannable. The reviewer should be able to tell at a glance what to double-check.

## What this skill does NOT do

- Does not produce dental, vision, or ancillary comparisons (template is medical-only).
- Does not produce age-banded rate sheets (the current template is composite/tier-rated only).
- Does not recommend a plan or carrier — output is the comparison, judgment is the user's.
- Does not modify the template structure, the formulas, or any branding/footer text.
- Does not fill in benefits the carrier didn't explicitly quote.

---

<!--
Bundled resources expected alongside this SKILL.md:

quote-spreadsheeter/
├── SKILL.md                              (this file)
└── assets/
    └── Prost11_Medical_Comparison.xlsx   (the master template — copy this each run)

Optional future additions:
- references/census-formats.md  — known census layouts from common HRIS systems (BambooHR, Gusto, ADP) with column maps
- references/carrier-quirks.md  — per-carrier plan naming conventions and common gotchas
-->

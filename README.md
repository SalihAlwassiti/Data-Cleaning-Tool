# Data Cleaning Tool

A high-speed desktop GUI application for inspecting, cleaning, deduplicating, and exporting large **Parquet** and **CSV** datasets. Built on **Python**, **Tkinter**, **DuckDB**, and **PyArrow**, it compiles visual cleaning rules into vectorized SQL pipelines that run out-of-core on millions of rows without exhausting system memory.

---

## Key Features

### Dataset Inspection & Profiling
* **Single-File Loading & Drag-and-Drop**: Open `.parquet`, `.csv`, `.tsv`, or `.csv.gz` files via the **Open File** dialog or by dragging and dropping a file directly onto the window (requires `tkinterdnd2`).
* **Instant Full-Dataset Metrics**: Computes total row count, total `NULL` cells, and per-column numeric summary statistics (`Avg`, `Med`, `Min`, `Max`) in a single pass when a file is loaded.
* **Color-Coded Data Preview**: Displays the first 500 rows with full-dataset per-column `Null %` and `Blank %` badges that scale smoothly in color from soft light red (`> 0%`) to saturated red (`≥ 20%`).
* **On-Demand Unique Value Counts**: Optionally calculates and displays exact per-column `Unique` counts in the preview header when **Show unique values per column in preview** is checked and **Preview Changes** is run—avoiding expensive `COUNT(DISTINCT)` scans during initial file loading.
* **Fast Horizontal Navigation**: Custom canvas-rendered column headers stay synchronized with the preview table and support accelerated horizontal scrolling via the scrollbar or `Shift + MouseWheel`.

### Per-Column Cleaning Rules
All transformations apply strictly to columns checked under **Keep**—unselected columns are visually dimmed, disabled, and excluded from processing and export:
* **Column Selection (`Keep`)**: Include or exclude individual columns, or toggle all columns at once using the header checkbox.
* **Whitespace Normalization (`WS`)**: Normalizes `\r\n` and `\r` line endings to `\n`, collapses consecutive spaces and tabs into a single space, and trims leading/trailing whitespace on text columns.
* **Case Conversion (`Lower` / `Upper`)**: Mutually exclusive lowercase or uppercase conversion on text columns.
* **Null Token Mapping (`Nulls -> NULL`)**: Converts custom comma-separated null tokens (default: `Null, NaN, None, N/A`), floating-point `NaN` / `Inf` / `-Inf`, and matching numeric sentinel values into SQL `NULL`.
* **Empty Value Collapsing (`Empty -> NULL`)**: Converts empty strings, whitespace-only strings, `[]`, `{}`, and empty lists/maps into SQL `NULL`.
* **Null Row Filtering (`Drop if NULL`)**: Drops any row where the marked column evaluates to `NULL` (evaluated after `Nulls -> NULL` and `Empty -> NULL`).
* **Column Deduplication (`Dedup Col`)**: Marks columns to serve as deduplication keys.
* **Ordered Multi-Value Find & Replace**: Supports single values or comma-separated lists in **Find** and **Replace With** (e.g., `Male, Female` → `M, F`), applying each pair sequentially in exact order across text, numeric, temporal, or boolean columns.
* **Bulk Rule Management**: Use the **Cleaning Rules (All)** header dropdown to toggle or clear rules across all selected columns simultaneously.

### Global Rules & Deduplication
* **Custom Null Tokens**: Define comma-separated null strings (supports CSV quoting for tokens containing commas) with optional **Case-insensitive** matching.
* **Full-Row Deduplication**: Check **Remove duplicate rows** to eliminate exact duplicate rows across all selected columns.
* **Custom Tie-Breaking (`Deduplicate keeping first by`)**: Keep the first matching record either by original row order (`By Order`) or sorted ascending by a specific selected date/timestamp column (automatically excluding that chosen date column from duplicate comparison keys).
* **Column Deduplication Modes**:
  * **Composite Key**: Deduplicates using the combined tuple of all columns marked `Dedup Col`.
  * **Per Column (Sequential)**: Deduplicates one marked column at a time in top-to-bottom table order.

### Pipeline Visibility & Safe Export
* **Live Cleaning Steps Window**: Toggle the **Cleaning Steps** button in the top bar to open a floating window listing every active transformation in the exact order DuckDB executes it.
* **Flexible Export Scoping**: Export **All Rows**, **First X Rows**, or a **Random Sample (%)**.
* **Output Formats**: Save cleaned datasets as **Parquet** (ZSTD-compressed) or **CSV** (with headers).
* **Non-Blocking & Atomic Writes**: Runs queries on background worker threads with a **Cancel** button (`duckdb.Connection.interrupt()`), writes to a temporary file before atomically replacing the target path, and prevents overwriting the active input file.

---

## Requirements & Installation

* **Python**: 3.9+ (with `tkinter` enabled)
* **Core Dependencies**:
  * `duckdb`
  * `pyarrow`
* **Optional Dependency** (for drag-and-drop file loading):
  * `tkinterdnd2`

Install all dependencies with `pip`:

```bash
pip install duckdb pyarrow tkinterdnd2
```

---

## Usage Guide

1. **Start the application**:
   ```bash
   python data_cleaning_tool.py
   ```
2. **Load a file**: Click **Open File** (or drag and drop a `.parquet` or `.csv` file onto the window).
3. **Configure column & global rules**:
   * Uncheck **Keep** on columns you want to drop from the dataset.
   * Open any column's **Cleaning Rules** dropdown (or **Cleaning Rules (All)** in the header) to enable rules.
   * Enter single or comma-separated replacement pairs in **Find** and **Replace With** (for example: `Unknown, N/A` in **Find** and `Other, Other` in **Replace With**).
   * Configure **Global Rules** for null strings, case sensitivity, unique count previews, and deduplication behavior.
4. **Inspect the execution order**: Click **Cleaning Steps** in the top toolbar to view the live, numbered list of active operations.
5. **Preview changes**: Click **Preview Changes** to inspect the first 500 cleaned rows along with full-dataset `Before` / `After` row counts, `NULL` cell counts, and per-column `Null %` / `Blank %` badges.
6. **Clean & Export**: Choose your **Export** scope (`All Rows`, `First X Rows`, or `Random Sample (%)`), select **Output** format (`Parquet` or `CSV`), and click **Clean & Export...**.

---

## Per-Column Rule Reference

| Rule | Short Badge | Target Types | Behavior |
| :--- | :--- | :--- | :--- |
| **Whitespace** | `WS` | Text | Normalizes `\r\n`/`\r` to `\n`, collapses repeated spaces/tabs, and trims ends. |
| **Lowercase** | `Lower` | Text | Converts text to lowercase (mutually exclusive with **Uppercase**). |
| **Uppercase** | `Upper` | Text | Converts text to uppercase (mutually exclusive with **Lowercase**). |
| **Nulls -> NULL** | `Null` | Text, Int, Float | Converts configured null tokens, `NaN`, `±Inf`, and matching numeric sentinels to `NULL`. |
| **Empty -> NULL** | `Empty` | Text, List, Map | Converts empty/blank strings, `[]`, `{}`, and empty containers to `NULL`. |
| **Drop if NULL** | `DropNULL` | All | Removes rows where the column is `NULL` after null/empty conversions. |
| **Dedup Col** | `Dedup` | All | Deduplicates rows using this column (via Composite Key or Sequential mode). |

---

## Execution Pipeline Order

Every **Preview Changes** and **Clean & Export** action compiles into a deterministic sequence of DuckDB Common Table Expressions (CTEs) executed in the exact order below:

```text
1. base              -> Project selected ('Keep') columns & assign original row index
2. step_ws           -> Normalize & trim whitespace on selected text columns
3. step_replace      -> Apply ordered comma-separated Find & Replace pairs per column
4. step_case         -> Apply Lowercase or Uppercase conversion on selected text columns
5. step_null         -> Convert matching null tokens, NaN/Inf, & numeric sentinels to NULL
6. step_empty        -> Convert empty/whitespace strings & empty lists/maps to NULL
7. step_filtered     -> Drop rows where any 'Drop if NULL' column is NULL
8. step_dedup_cols   -> Deduplicate by Composite Key or Sequential Per-Column passes
9. step_dedup_full   -> Remove duplicate full rows across selected columns
10. Final Query      -> Restore original row order & apply LIMIT or random() sampling
```

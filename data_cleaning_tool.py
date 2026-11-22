import csv
from decimal import Decimal, InvalidOperation
import io
import math
import os
import re
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont
import duckdb
import pyarrow.parquet as pq

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except ImportError:
    HAS_DND = False

PREVIEW_ROW_LIMIT = 500
HSCROLL_UNIT_MULTIPLIER = 16

DEFAULT_NULL_VALUES_TEXT = "Null, NaN, None, N/A"

OUTPUT_FORMATS = [
    "Parquet",
    "CSV",
]

EXPORT_LIMIT_MODES = [
    "All Rows",
    "First X Rows",
    "Random Sample (%)",
]

DEDUP_MODES = [
    "Composite Key",
    "Per Column (Sequential)",
]

RULE_OPTIONS = [
    ("ws_var", "Whitespace", "WS"),
    ("lower_var", "Lowercase", "Lower"),
    ("upper_var", "Uppercase", "Upper"),
    ("null_var", "Nulls -> NULL", "Null"),
    ("empty_var", "Empty -> NULL", "Empty"),
    ("drop_null_var", "Drop if NULL", "DropNULL"),
    ("dedup_var", "Dedup Col", "Dedup"),
]

NO_NULL_SENTINEL = "__DUCKDB_NO_NULL_9F8E7D__"
SCROLL_BIND_TAG = "RulesCanvasScroll"

selected_source = None
source_relation_sql = ""
total_source_rows = 0
raw_total_nulls = 0
row_widget_refs = []
header_rules_mb = None
header_rule_vars = {}
header_keep_var = None
header_keep_chk = None
latest_preview_cols = []
latest_preview_col_stats = {}
steps_window_visible = False

con_lock = threading.Lock()
active_duckdb_con = None
is_busy = False


def quote_ident(identifier):
    """Double-quote a SQL column identifier safely for DuckDB."""
    return '"' + str(identifier).replace('"', '""') + '"'


def quote_literal(value):
    """Single-quote a SQL string literal safely."""
    return "'" + str(value).replace("'", "''") + "'"


def escape_duckdb_glob(path_str):
    """Escape glob metacharacters (*, ?, [) so literal brackets/wildcards in paths open safely."""
    return (
        path_str.replace("[", "[[]")
        .replace("*", "[*]")
        .replace("?", "[?]")
    )


def make_unique_alias(base_name, existing_cols):
    """Return a SQL column alias guaranteed not to collide with dataset column names."""
    existing_lower = {c.lower() for c in existing_cols}
    candidate = base_name
    counter = 1
    while candidate.lower() in existing_lower:
        candidate = f"{base_name}_{counter}"
        counter += 1
    return candidate


def classify_data_type(type_str):
    """Classify PyArrow or DuckDB type strings without substring false positives."""
    t = str(type_str).upper().strip()

    # Unwrap PyArrow dictionary<values=..., indices=..., ordered=...> to its value type
    if t.startswith("DICTIONARY<"):
        m = re.search(r"VALUES\s*=\s*(.+?)(?:,\s*INDICES\s*=|>)", t)
        if m:
            return classify_data_type(m.group(1).strip())
        return "other"

    if any(k in t for k in ("LIST", "ARRAY", "MAP", "[]")):
        return "list_or_map"
    if "STRUCT" in t:
        return "struct"
    if "BOOL" in t:
        return "boolean"
    if not t.startswith("INTERVAL") and any(k in t for k in ("DATE", "TIMESTAMP", "TIME")):
        return "temporal"
    if re.search(r"\b(FLOAT|FLOAT16|FLOAT32|FLOAT64|HALFFLOAT|DOUBLE|REAL)\b", t):
        return "float"
    if re.search(
        r"\b(INT|INT8|INT16|INT32|INT64|UINT8|UINT16|UINT32|UINT64|"
        r"TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|"
        r"UINTEGER|UBIGINT|UHUGEINT|DECIMAL|NUMERIC)\b",
        t,
    ):
        return "integer"
    if re.search(r"\b(VARCHAR|CHAR|TEXT|STRING|LARGE_STRING|UTF8|LARGE_UTF8)\b", t):
        return "text"
    return "other"


def is_numeric_category(category):
    return category in ("integer", "float")


def format_duration(seconds):
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60.0:
        return f"{seconds:.2f}s"
    mins, secs = divmod(seconds, 60)
    return f"{int(mins)}m {secs:.1f}s"


def format_preview_cell(val):
    if val is None:
        return "NULL"
    s = str(val).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if s == "":
        return '"" (empty)'
    return s


def format_numeric_stat(val):
    if val is None:
        return "NULL"
    if isinstance(val, int) and not isinstance(val, bool):
        return f"{val:,}"
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return str(val)
    if math.isnan(fval):
        return "NaN"
    if math.isinf(fval):
        return "Inf" if fval > 0 else "-Inf"
    if fval == 0.0:
        return "0"
    if fval.is_integer() and abs(fval) < 1e15:
        return f"{int(round(fval)):,}"
    if abs(fval) < 0.01 or abs(fval) >= 1e12:
        return f"{fval:.4g}"
    return f"{fval:,.2f}".rstrip("0").rstrip(".")


def format_pct(count, total):
    if total <= 0 or count <= 0:
        return "0%"
    if count >= total:
        return "100%"
    pct = (count / total) * 100.0
    if pct < 0.1:
        return "<0.1%"
    return f"{pct:.1f}%"


def get_pct_red_colors(pct_val):
    """Return (bg_hex, fg_hex) scaling smoothly from light red (>0%) to max red (>=20%)."""
    if pct_val <= 0.0:
        return "#f0f0f0", "#555555"

    t = min(pct_val / 20.0, 1.0)
    # Interpolate from soft light red (#ffe4e4) to saturated red (#dc2626)
    r = round(255 + (220 - 255) * t)
    g = round(228 + (38 - 228) * t)
    b = round(228 + (38 - 228) * t)
    bg_hex = f"#{r:02x}{g:02x}{b:02x}"
    fg_hex = "#ffffff" if t >= 0.55 else "#7f1d1d"
    return bg_hex, fg_hex


def parse_null_tokens(raw_text):
    """Parse comma-separated custom null tokens."""
    text = raw_text.strip()
    if not text:
        return []
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    row = next(reader, [])
    tokens = []
    seen = set()
    for item in row:
        if item != "" and item not in seen:
            seen.add(item)
            tokens.append(item)
    return tokens


def parse_find_replace_pairs(col_name, find_raw, replace_raw):
    """Parse comma-separated Find and Replace With inputs into ordered (find, replace) pairs."""
    if find_raw == "":
        return []

    find_reader = csv.reader(io.StringIO(find_raw), skipinitialspace=True)
    find_items = next(find_reader, [])
    if not find_items:
        return []

    if any(item == "" for item in find_items):
        raise ValueError(
            f"Column '{col_name}': Find values cannot be empty (check for trailing or double commas)."
        )

    if replace_raw == "":
        if len(find_items) == 1:
            replace_items = [""]
        else:
            raise ValueError(
                f"Column '{col_name}': Find has {len(find_items)} values, so Replace With must also have "
                f"{len(find_items)} comma-separated values."
            )
    else:
        repl_reader = csv.reader(io.StringIO(replace_raw), skipinitialspace=True)
        replace_items = next(repl_reader, [])

    if len(find_items) != len(replace_items):
        raise ValueError(
            f"Column '{col_name}': Find has {len(find_items)} value(s) ({', '.join(find_items)}) "
            f"but Replace With has {len(replace_items)} value(s). Counts must match in order."
        )

    return list(zip(find_items, replace_items))


def extract_numeric_null_literals(null_tokens, category, duck_type):
    """Build SQL literal expressions for numeric tokens matching integer or float columns."""
    sql_items = []
    seen = set()
    for tok in null_tokens:
        cleaned = tok.strip()
        if not cleaned:
            continue
        if category == "integer":
            try:
                dec = Decimal(cleaned)
            except InvalidOperation:
                continue
            if not dec.is_finite() or dec != dec.to_integral_value():
                continue
            int_str = str(int(dec))
            if int_str not in seen:
                seen.add(int_str)
                sql_items.append(f"TRY_CAST({quote_literal(int_str)} AS {duck_type})")
        elif category == "float":
            try:
                fval = float(cleaned)
            except ValueError:
                continue
            if not math.isfinite(fval):
                continue
            if cleaned not in seen:
                seen.add(cleaned)
                sql_items.append(f"TRY_CAST({quote_literal(cleaned)} AS {duck_type})")
    return sql_items


def fetch_arrow_compat(con, sql):
    """Execute SQL and return a PyArrow Table across all DuckDB 1.x versions."""
    rel = con.sql(sql)
    if hasattr(rel, "to_arrow_table"):
        return rel.to_arrow_table()
    if hasattr(rel, "fetch_arrow_table"):
        return rel.fetch_arrow_table()
    return rel.arrow().read_all()


def compute_column_preview_stats(con, relation_expr, col_cat_pairs, include_unique=False):
    """Compute full-dataset null %, blank %, and optional unique counts per column in one pass."""
    if not col_cat_pairs:
        return {}

    select_parts = ["COUNT(*)"]
    for col_name, cat in col_cat_pairs:
        q_col = quote_ident(col_name)
        null_expr = f"SUM(CASE WHEN {q_col} IS NULL THEN 1 ELSE 0 END)"
        if cat == "text":
            blank_expr = f"SUM(CASE WHEN {q_col} IS NOT NULL AND regexp_matches({q_col}, '^\\s*$') THEN 1 ELSE 0 END)"
        elif cat == "list_or_map":
            blank_expr = f"SUM(CASE WHEN {q_col} IS NOT NULL AND len({q_col}) = 0 THEN 1 ELSE 0 END)"
        else:
            blank_expr = "0"

        if include_unique:
            select_parts.extend([null_expr, blank_expr, f"COUNT(DISTINCT {q_col})"])
        else:
            select_parts.extend([null_expr, blank_expr])

    row = con.execute(f"SELECT {', '.join(select_parts)} FROM {relation_expr}").fetchall()[0]
    total = row[0] or 0
    stats_map = {}
    offset = 1
    stride = 3 if include_unique else 2
    for col_name, _ in col_cat_pairs:
        null_cnt = row[offset] or 0
        blank_cnt = row[offset + 1] or 0
        uniq_cnt = (row[offset + 2] or 0) if include_unique else None
        offset += stride

        null_pct_val = ((null_cnt / total) * 100.0) if (total > 0 and null_cnt > 0) else 0.0
        blank_pct_val = ((blank_cnt / total) * 100.0) if (total > 0 and blank_cnt > 0) else 0.0

        stats_map[col_name] = {
            "null_pct": format_pct(null_cnt, total),
            "null_pct_val": null_pct_val,
            "blank_pct": format_pct(blank_cnt, total),
            "blank_pct_val": blank_pct_val,
            "unique_count": uniq_cnt,
        }
    return stats_map


def get_pyarrow_parquet_schema_map(source_path):
    """Read PyArrow field dtypes directly from Parquet metadata."""
    if not source_path.lower().endswith(".parquet"):
        return {}
    try:
        pa_schema = pq.read_schema(source_path)
        return {field.name: str(field.type) for field in pa_schema}
    except Exception:
        return {}


def build_source_relation_sql(source_path):
    """Build a glob-safe DuckDB table-function expression keeping empty CSV strings as ''."""
    norm = source_path.replace("\\", "/")
    escaped_file = escape_duckdb_glob(norm)
    if norm.lower().endswith(".parquet"):
        return f"read_parquet({quote_literal(escaped_file)})"
    return f"read_csv_auto({quote_literal(escaped_file)}, nullstr=[{quote_literal(NO_NULL_SENTINEL)}])"


def load_source(source_path):
    global is_busy

    if is_busy:
        return
    if not os.path.exists(source_path):
        messagebox.showerror("Not Found", f"File does not exist:\n{source_path}")
        return
    if os.path.isdir(source_path):
        messagebox.showerror("Invalid File", "Folder input is not supported. Please select a single .parquet or .csv file.")
        return
    if not source_path.lower().endswith((".parquet", ".csv", ".tsv", ".csv.gz")):
        messagebox.showerror("Unsupported File", "Please select a single .parquet or .csv file.")
        return

    rel_sql = build_source_relation_sql(source_path)

    is_busy = True
    set_controls_state("disabled")
    upload_btn.config(state="disabled")
    cancel_btn.config(state="normal")
    status_label.config(text="Inspecting source schema, stats, and row count...", fg="#1f6aa5")

    threading.Thread(
        target=_load_worker,
        args=(source_path, rel_sql),
        daemon=True,
    ).start()


def _load_worker(source_path, rel_sql):
    global active_duckdb_con
    con = None
    start = time.perf_counter()
    try:
        con = duckdb.connect(database=":memory:")
        with con_lock:
            active_duckdb_con = con

        desc_rows = con.execute(f"DESCRIBE SELECT * FROM {rel_sql}").fetchall()
        if not desc_rows:
            raise ValueError("No columns were found in the selected file.")

        is_parquet_input = source_path.lower().endswith(".parquet")
        pa_dtype_map = get_pyarrow_parquet_schema_map(source_path) if is_parquet_input else {}
        if is_parquet_input and not pa_dtype_map:
            empty_arrow = fetch_arrow_compat(con, f"SELECT * FROM {rel_sql} LIMIT 0")
            pa_dtype_map = {field.name: str(field.type) for field in empty_arrow.schema}

        col_specs = []
        for d in desc_rows:
            col_name = d[0]
            duck_type = d[1]
            display_type = pa_dtype_map.get(col_name, duck_type) if is_parquet_input else duck_type
            category = classify_data_type(duck_type)
            col_specs.append(
                {
                    "col": col_name,
                    "duck_type": duck_type,
                    "display_type": display_type,
                    "category": category,
                }
            )

        null_sum_expr = " + ".join(
            f"(CASE WHEN {quote_ident(spec['col'])} IS NULL THEN 1 ELSE 0 END)" for spec in col_specs
        )

        # Compute row count, total NULLs, and full-dataset numeric stats (avg, median, min, max) in one scan
        numeric_cols = []
        stat_select_parts = [
            "COUNT(*)",
            f"COALESCE(SUM({null_sum_expr}), 0)",
        ]
        for spec in col_specs:
            col_name = spec["col"]
            cat = spec["category"]
            if is_numeric_category(cat):
                numeric_cols.append(col_name)
                q_col = quote_ident(col_name)
                safe_expr = f"CASE WHEN isnan({q_col}) THEN NULL ELSE {q_col} END" if cat == "float" else q_col
                stat_select_parts.extend(
                    [
                        f"AVG({safe_expr})",
                        f"MEDIAN({safe_expr})",
                        f"MIN({safe_expr})",
                        f"MAX({safe_expr})",
                    ]
                )

        agg_row = con.execute(f"SELECT {', '.join(stat_select_parts)} FROM {rel_sql}").fetchall()[0]
        total_rows = agg_row[0]
        total_nulls = agg_row[1]

        numeric_stats_map = {}
        offset = 2
        for col_name in numeric_cols:
            avg_v, med_v, min_v, max_v = agg_row[offset : offset + 4]
            offset += 4
            numeric_stats_map[col_name] = (
                f"Avg: {format_numeric_stat(avg_v)} | "
                f"Med: {format_numeric_stat(med_v)} | "
                f"Min: {format_numeric_stat(min_v)} | "
                f"Max: {format_numeric_stat(max_v)}"
            )

        col_cat_pairs = [(spec["col"], spec["category"]) for spec in col_specs]
        col_preview_stats = compute_column_preview_stats(con, rel_sql, col_cat_pairs, include_unique=False)

        sample_table = fetch_arrow_compat(con, f"SELECT * FROM {rel_sql} LIMIT {PREVIEW_ROW_LIMIT}")
        elapsed = time.perf_counter() - start
        root.after(
            0,
            _on_load_done,
            source_path,
            rel_sql,
            col_specs,
            total_rows,
            total_nulls,
            numeric_stats_map,
            col_preview_stats,
            sample_table,
            elapsed,
        )
    except Exception as e:
        root.after(0, _on_worker_error, str(e))
    finally:
        with con_lock:
            if active_duckdb_con is con:
                active_duckdb_con = None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _on_load_done(
    source_path,
    rel_sql,
    col_specs,
    total_rows,
    total_nulls,
    numeric_stats_map,
    col_preview_stats,
    sample_table,
    elapsed,
):
    global selected_source, source_relation_sql, total_source_rows, raw_total_nulls, is_busy

    is_busy = False
    selected_source = source_path
    source_relation_sql = rel_sql
    total_source_rows = total_rows
    raw_total_nulls = total_nulls

    display_name = os.path.basename(os.path.normpath(source_path))
    file_label.config(text=f"{display_name}  ({total_source_rows:,} total rows, {len(col_specs)} cols)", fg="#111111")

    display_info_map = {}
    for col_name in sample_table.column_names:
        if col_name in numeric_stats_map:
            display_info_map[col_name] = numeric_stats_map[col_name]
        else:
            non_nulls = [v for v in sample_table[col_name].to_pylist() if v is not None]
            display_info_map[col_name] = format_preview_cell(non_nulls[0]) if non_nulls else "NULL"

    dedup_mode_var.set("Composite Key")
    row_dedup_var.set(False)
    case_insensitive_var.set(False)
    show_unique_var.set(False)
    export_limit_mode_var.set("All Rows")
    export_limit_val_var.set("")

    build_column_rows(col_specs, display_info_map)
    sync_date_dedup_options(reset_to_default=True)
    render_treeview(preview_tree, sample_table, col_preview_stats)
    refresh_steps_window()

    preview_info_label.config(
        text=(
            f"Showing first {PREVIEW_ROW_LIMIT} rows   |   "
            f"Full Dataset — Before: {total_source_rows:,} rows  |  "
            f"After: — (click 'Preview Changes')  |  "
            f"NULL cells: {raw_total_nulls:,}"
        ),
        fg="#111111",
    )

    set_controls_state("normal")
    upload_btn.config(state="normal")
    cancel_btn.config(state="disabled")
    status_label.config(
        text=f"Loaded in {format_duration(elapsed)}.",
        fg="#555555",
    )


def on_drop(event):
    if is_busy:
        return
    paths = root.tk.splitlist(event.data)
    if paths:
        load_source(paths[0].strip())


def upload_file():
    if is_busy:
        return
    path = filedialog.askopenfilename(
        title="Select a Parquet or CSV file",
        filetypes=[
            ("Parquet & CSV Files", "*.parquet *.csv *.tsv *.csv.gz"),
            ("Parquet Files", "*.parquet"),
            ("CSV Files", "*.csv *.tsv *.csv.gz"),
            ("All Files", "*.*"),
        ],
    )
    if path:
        load_source(path)


def on_export_limit_mode_changed(event=None):
    if is_busy or not selected_source:
        export_limit_val_entry.config(state="disabled")
        refresh_steps_window()
        return
    mode = export_limit_mode_var.get()
    if mode == "All Rows":
        export_limit_val_entry.config(state="disabled")
    elif mode == "First X Rows":
        export_limit_val_entry.config(state="normal")
        if not export_limit_val_var.get().strip():
            export_limit_val_var.set("1000")
    else:
        export_limit_val_entry.config(state="normal")
        if not export_limit_val_var.get().strip():
            export_limit_val_var.set("10")
    refresh_steps_window()


def parse_export_limit():
    """Validate and return (mode, numeric_value) for export limiting."""
    mode = export_limit_mode_var.get().strip() or "All Rows"
    if mode == "All Rows":
        return "all", None

    raw = export_limit_val_var.get().strip()
    if not raw:
        raise ValueError(f"Please enter a value for '{mode}'.")

    if mode == "First X Rows":
        try:
            count = int(raw)
        except ValueError:
            raise ValueError("'First X Rows' must be a positive whole number (e.g. 1000).")
        if count <= 0:
            raise ValueError("'First X Rows' must be greater than 0.")
        return "first_x", count

    if mode == "Random Sample (%)":
        try:
            pct = float(raw)
        except ValueError:
            raise ValueError("'Random Sample (%)' must be a number between 0 and 100 (e.g. 10 or 2.5).")
        if not math.isfinite(pct) or pct <= 0.0 or pct > 100.0:
            raise ValueError("'Random Sample (%)' must be greater than 0 and at most 100.")
        return "random_pct", pct

    return "all", None


def set_controls_state(state):
    has_source = bool(selected_source)
    effective = state if has_source else "disabled"
    preview_btn.config(state=effective)
    export_btn.config(state=effective)
    null_values_entry.config(state=effective)
    case_insensitive_chk.config(state=effective)
    show_unique_chk.config(state=effective)
    row_dedup_chk.config(state=effective)
    first_by_cb.config(state="readonly" if effective == "normal" else "disabled")
    dedup_mode_cb.config(state="readonly" if effective == "normal" else "disabled")
    out_format_cb.config(state="readonly" if effective == "normal" else "disabled")
    export_limit_mode_cb.config(state="readonly" if effective == "normal" else "disabled")

    if effective == "normal" and export_limit_mode_var.get() != "All Rows":
        export_limit_val_entry.config(state="normal")
    else:
        export_limit_val_entry.config(state="disabled")

    if header_keep_chk is not None:
        header_keep_chk.config(state=effective)
    if header_rules_mb is not None:
        header_rules_mb.config(state=effective)

    for row in row_widget_refs:
        row["keep_chk"].config(state=effective)
        row_effective = effective if row["keep_var"].get() else "disabled"
        row["rules_mb"].config(state=row_effective)
        row["find_entry"].config(state=row_effective)
        row["replace_entry"].config(state=row_effective)


def _update_row_rules_button(row):
    active_labels = [label for var_key, label, _ in RULE_OPTIONS if row[var_key].get()]
    active_short = [short for var_key, _, short in RULE_OPTIONS if row[var_key].get()]

    count = len(active_labels)
    if count == 0:
        row["rules_mb"].config(text="None ▾", fg="#666666")
    elif count == 1:
        row["rules_mb"].config(text=f"{active_labels[0]} ▾", fg="#111111")
    elif count <= 4:
        row["rules_mb"].config(text=f"{', '.join(active_short)} ▾", fg="#111111")
    else:
        row["rules_mb"].config(text=f"{count} Rules ▾", fg="#111111")


def _apply_row_keep_visual_state(row):
    is_kept = row["keep_var"].get()
    if is_kept:
        row["col_label"].config(fg="#111111")
        row["type_label"].config(fg="#555555")
        row["info_label"].config(fg="#333333" if is_numeric_category(row["category"]) else "#555555")
        if not is_busy and selected_source:
            row["rules_mb"].config(state="normal")
            row["find_entry"].config(state="normal")
            row["replace_entry"].config(state="normal")
        _update_row_rules_button(row)
    else:
        row["col_label"].config(fg="#999999")
        row["type_label"].config(fg="#aaaaaa")
        row["info_label"].config(fg="#aaaaaa")
        row["rules_mb"].config(state="disabled", fg="#999999")
        row["find_entry"].config(state="disabled")
        row["replace_entry"].config(state="disabled")


def sync_date_dedup_options(reset_to_default=False):
    kept_date_cols = [
        r["col"] for r in row_widget_refs if r["keep_var"].get() and r["category"] == "temporal"
    ]
    options = ["By Order"] + kept_date_cols
    first_by_cb.config(values=options)
    current_val = first_by_var.get()
    if reset_to_default or current_val not in options:
        first_by_var.set("By Order")


def _sync_all_headers():
    if not row_widget_refs:
        return
    if header_keep_var is not None:
        header_keep_var.set(all(r["keep_var"].get() for r in row_widget_refs))
    kept_rows = [r for r in row_widget_refs if r["keep_var"].get()]
    for var_key, _, _ in RULE_OPTIONS:
        if var_key in header_rule_vars:
            header_rule_vars[var_key].set(bool(kept_rows) and all(r[var_key].get() for r in kept_rows))


def on_row_keep_changed(row):
    _apply_row_keep_visual_state(row)
    _sync_all_headers()
    sync_date_dedup_options(reset_to_default=False)
    refresh_steps_window()


def on_row_rule_changed(row, changed_key=None):
    # Keep Lowercase and Uppercase mutually exclusive per column
    if changed_key == "lower_var" and row["lower_var"].get():
        row["upper_var"].set(False)
    elif changed_key == "upper_var" and row["upper_var"].get():
        row["lower_var"].set(False)

    _update_row_rules_button(row)
    _sync_all_headers()
    refresh_steps_window()


def clear_row_rules(row):
    if is_busy:
        return
    for var_key, _, _ in RULE_OPTIONS:
        row[var_key].set(False)
    on_row_rule_changed(row)


def on_header_keep_toggle():
    if is_busy or header_keep_var is None:
        return
    new_val = header_keep_var.get()
    for row in row_widget_refs:
        row["keep_var"].set(new_val)
        _apply_row_keep_visual_state(row)
    _sync_all_headers()
    sync_date_dedup_options(reset_to_default=False)
    refresh_steps_window()


def on_header_rule_toggle(var_key):
    if is_busy or var_key not in header_rule_vars:
        return
    new_val = header_rule_vars[var_key].get()

    # Keep Lowercase and Uppercase mutually exclusive on bulk header toggle
    if var_key == "lower_var" and new_val and "upper_var" in header_rule_vars:
        header_rule_vars["upper_var"].set(False)
    elif var_key == "upper_var" and new_val and "lower_var" in header_rule_vars:
        header_rule_vars["lower_var"].set(False)

    for row in row_widget_refs:
        if not row["keep_var"].get():
            continue
        row[var_key].set(new_val)
        if var_key == "lower_var" and new_val:
            row["upper_var"].set(False)
        elif var_key == "upper_var" and new_val:
            row["lower_var"].set(False)
        _update_row_rules_button(row)
    _sync_all_headers()
    refresh_steps_window()


def clear_all_column_rules():
    if is_busy:
        return
    for var_key, _, _ in RULE_OPTIONS:
        if var_key in header_rule_vars:
            header_rule_vars[var_key].set(False)
    for row in row_widget_refs:
        for var_key, _, _ in RULE_OPTIONS:
            row[var_key].set(False)
        _update_row_rules_button(row)
    refresh_steps_window()


def attach_scroll_tag(widget):
    widget.bindtags((SCROLL_BIND_TAG,) + widget.bindtags())


def build_column_rows(col_specs, display_info_map):
    global header_rules_mb, header_keep_var, header_keep_chk

    for widget in rows_frame.winfo_children():
        widget.destroy()
    row_widget_refs.clear()
    header_rule_vars.clear()

    # Row 0: Table Headers
    header_keep_var = tk.BooleanVar(value=True)
    header_keep_chk = tk.Checkbutton(
        rows_frame,
        text="Keep",
        variable=header_keep_var,
        font=("Segoe UI", 8, "bold"),
        command=on_header_keep_toggle,
    )
    header_keep_chk.grid(row=0, column=0, padx=6, pady=(0, 6))
    attach_scroll_tag(header_keep_chk)

    text_headers = [
        (1, "Column", 20),
        (2, "Type", 18),
        (3, "Sample Value / Numeric Stats", 40),
        (5, "Find (comma-sep)", 23),
        (6, "Replace With (comma-sep)", 23),
    ]
    for col_idx, label_text, width in text_headers:
        lbl = tk.Label(rows_frame, text=label_text, font=("Segoe UI", 8, "bold"), width=width, anchor="w")
        lbl.grid(row=0, column=col_idx, padx=6, pady=(0, 6), sticky="w")
        attach_scroll_tag(lbl)

    # Header-level multi-select dropdown to bulk-toggle cleaning rules across all selected columns
    header_rules_mb = tk.Menubutton(
        rows_frame,
        text="Cleaning Rules (All) ▾",
        relief="raised",
        bd=1,
        indicatoron=False,
        anchor="w",
        padx=8,
        pady=3,
        width=28,
        font=("Segoe UI", 8, "bold"),
    )
    header_rules_menu = tk.Menu(header_rules_mb, tearoff=0, font=("Segoe UI", 8))
    for var_key, label_text, _ in RULE_OPTIONS:
        h_var = tk.BooleanVar(value=False)
        header_rule_vars[var_key] = h_var
        header_rules_menu.add_checkbutton(
            label=label_text,
            variable=h_var,
            command=lambda vk=var_key: on_header_rule_toggle(vk),
        )
    header_rules_menu.add_separator()
    header_rules_menu.add_command(label="Clear all rules", command=clear_all_column_rules)
    header_rules_mb.config(menu=header_rules_menu)
    header_rules_mb.grid(row=0, column=4, padx=6, pady=(0, 6), sticky="w")
    attach_scroll_tag(header_rules_mb)

    for idx, spec in enumerate(col_specs, start=1):
        col_name = spec["col"]
        duck_type = spec["duck_type"]
        display_type = spec["display_type"]
        category = spec["category"]
        info_val = display_info_map.get(col_name, "")

        keep_var = tk.BooleanVar(value=True)
        ws_var = tk.BooleanVar(value=False)
        lower_var = tk.BooleanVar(value=False)
        upper_var = tk.BooleanVar(value=False)
        null_var = tk.BooleanVar(value=False)
        empty_var = tk.BooleanVar(value=False)
        drop_null_var = tk.BooleanVar(value=False)
        dedup_var = tk.BooleanVar(value=False)
        find_var = tk.StringVar(value="")
        replace_var = tk.StringVar(value="")

        find_var.trace_add("write", lambda *_args: refresh_steps_window())
        replace_var.trace_add("write", lambda *_args: refresh_steps_window())

        keep_chk = tk.Checkbutton(rows_frame, variable=keep_var)
        col_label = tk.Label(rows_frame, text=col_name, font=("Segoe UI", 9), width=20, anchor="w", fg="#111111")
        short_dtype = (display_type[:20] + "…") if len(display_type) > 21 else display_type
        type_label = tk.Label(rows_frame, text=short_dtype, font=("Consolas", 8), width=18, anchor="w", fg="#555555")

        if is_numeric_category(category):
            display_text = info_val
        else:
            display_text = (info_val[:38] + "…") if len(info_val) > 39 else info_val

        info_label = tk.Label(
            rows_frame,
            text=display_text,
            font=("Consolas", 8),
            width=40,
            anchor="w",
            fg="#333333" if is_numeric_category(category) else "#555555",
        )

        rules_mb = tk.Menubutton(
            rows_frame,
            text="None ▾",
            relief="groove",
            bd=1,
            indicatoron=False,
            anchor="w",
            padx=8,
            pady=3,
            width=28,
            font=("Segoe UI", 8),
            fg="#666666",
            bg="#ffffff",
        )
        rules_menu = tk.Menu(rules_mb, tearoff=0, font=("Segoe UI", 8))

        # 50% larger input boxes (23 chars vs previous 15 chars)
        find_entry = tk.Entry(rows_frame, textvariable=find_var, font=("Consolas", 8), width=23)
        replace_entry = tk.Entry(rows_frame, textvariable=replace_var, font=("Consolas", 8), width=23)

        row_item = {
            "col": col_name,
            "duck_type": duck_type,
            "display_type": display_type,
            "category": category,
            "keep_var": keep_var,
            "ws_var": ws_var,
            "lower_var": lower_var,
            "upper_var": upper_var,
            "null_var": null_var,
            "empty_var": empty_var,
            "drop_null_var": drop_null_var,
            "dedup_var": dedup_var,
            "find_var": find_var,
            "replace_var": replace_var,
            "keep_chk": keep_chk,
            "col_label": col_label,
            "type_label": type_label,
            "info_label": info_label,
            "rules_mb": rules_mb,
            "find_entry": find_entry,
            "replace_entry": replace_entry,
        }

        keep_chk.config(command=lambda r=row_item: on_row_keep_changed(r))

        for var_key, label_text, _ in RULE_OPTIONS:
            rules_menu.add_checkbutton(
                label=label_text,
                variable=row_item[var_key],
                command=lambda r=row_item, vk=var_key: on_row_rule_changed(r, vk),
            )
        rules_menu.add_separator()
        rules_menu.add_command(label="Clear all", command=lambda r=row_item: clear_row_rules(r))
        rules_mb.config(menu=rules_menu)

        for w in (keep_chk, col_label, type_label, info_label, rules_mb, find_entry, replace_entry):
            attach_scroll_tag(w)

        keep_chk.grid(row=idx, column=0, padx=6, pady=2)
        col_label.grid(row=idx, column=1, padx=6, pady=2, sticky="w")
        type_label.grid(row=idx, column=2, padx=6, pady=2, sticky="w")
        info_label.grid(row=idx, column=3, padx=6, pady=2, sticky="w")
        rules_mb.grid(row=idx, column=4, padx=6, pady=2, sticky="w")
        find_entry.grid(row=idx, column=5, padx=6, pady=2, sticky="w")
        replace_entry.grid(row=idx, column=6, padx=6, pady=2, sticky="w")

        row_widget_refs.append(row_item)

    rows_frame.update_idletasks()
    canvas.configure(scrollregion=canvas.bbox("all"))
    canvas.yview_moveto(0)


# --- Step-by-Step Cleaning Pipeline Description (Exact Code Execution Order) ---
def build_cleaning_steps_list():
    """Return (active_transform_count, ordered_step_strings) matching build_cleaning_sql() order."""
    if not selected_source or not row_widget_refs:
        return 0, []

    kept_rows = [r for r in row_widget_refs if r["keep_var"].get()]
    if not kept_rows:
        return 0, ["Warning: No columns are selected under 'Keep'."]

    steps = []
    transform_count = 0

    # Step 1: Base stream & column projection
    if len(kept_rows) == len(row_widget_refs):
        steps.append(f"Select all {len(kept_rows)} columns")
    else:
        kept_names = ", ".join(r["col"] for r in kept_rows)
        steps.append(f"Keep {len(kept_rows)} of {len(row_widget_refs)} columns: {kept_names}")
        transform_count += 1

    sim_categories = {r["col"]: r["category"] for r in kept_rows}

    # Step 2 (CTE step_ws): Whitespace canonicalization
    ws_cols = [r["col"] for r in kept_rows if r["ws_var"].get() and sim_categories[r["col"]] == "text"]
    if ws_cols:
        steps.append(f"Trim & normalize whitespace in column(s): {', '.join(ws_cols)}")
        transform_count += 1

    # Step 3 (CTE step_replace): Multi-value ordered Find & Replace
    replace_items_desc = []
    for r in kept_rows:
        col = r["col"]
        find_val = r["find_var"].get()
        repl_val = r["replace_var"].get()
        if find_val != "":
            try:
                pairs = parse_find_replace_pairs(col, find_val, repl_val)
                pair_str = ", ".join(f"'{f}' -> '{rep}'" for f, rep in pairs)
                replace_items_desc.append(f"{col} ({pair_str})")
            except ValueError as err:
                replace_items_desc.append(f"{col} [Invalid pairs: {err}]")
            if sim_categories[col] in ("integer", "float", "temporal", "boolean"):
                sim_categories[col] = "text"

    if replace_items_desc:
        steps.append(f"Find and replace in column(s): {'; '.join(replace_items_desc)}")
        transform_count += 1

    # Step 4 (CTE step_case): Lowercase / Uppercase
    lower_cols = [r["col"] for r in kept_rows if sim_categories[r["col"]] == "text" and r["lower_var"].get()]
    if lower_cols:
        steps.append(f"Convert to lowercase in column(s): {', '.join(lower_cols)}")
        transform_count += 1

    upper_cols = [r["col"] for r in kept_rows if sim_categories[r["col"]] == "text" and r["upper_var"].get()]
    if upper_cols:
        steps.append(f"Convert to uppercase in column(s): {', '.join(upper_cols)}")
        transform_count += 1

    # Step 5 (CTE step_null): Null values -> NULL
    null_cols = [r["col"] for r in kept_rows if r["null_var"].get()]
    if null_cols:
        tokens_str = null_values_var.get().strip() or "(none)"
        case_str = "case-insensitive" if case_insensitive_var.get() else "case-sensitive"
        steps.append(f"Convert null values [{tokens_str}] ({case_str}) to NULL in column(s): {', '.join(null_cols)}")
        transform_count += 1

    # Step 6 (CTE step_empty): Empty -> NULL
    empty_cols = [r["col"] for r in kept_rows if r["empty_var"].get()]
    if empty_cols:
        steps.append(f"Convert empty/blank values to NULL in column(s): {', '.join(empty_cols)}")
        transform_count += 1

    # Step 7 (CTE step_filtered): Drop rows if NULL
    drop_null_cols = [r["col"] for r in kept_rows if r["drop_null_var"].get()]
    if drop_null_cols:
        steps.append(f"Drop rows if NULL in column(s): {', '.join(drop_null_cols)}")
        transform_count += 1

    # Step 8 (CTE step_dedup_cols): Per-column / composite key deduplication
    first_by = first_by_var.get().strip() or "By Order"
    kept_date_cols = [r["col"] for r in kept_rows if r["category"] == "temporal"]
    use_specific_date = first_by != "By Order" and first_by in kept_date_cols
    order_desc = f"keeping first by '{first_by}' ascending" if use_specific_date else "keeping first by original row order"

    dedup_col_names = [r["col"] for r in kept_rows if r["dedup_var"].get()]
    if dedup_col_names:
        effective_dedup_cols = [c for c in dedup_col_names if c != first_by] if use_specific_date else dedup_col_names
        if effective_dedup_cols:
            if dedup_mode_var.get() == "Per Column (Sequential)":
                for col_name in effective_dedup_cols:
                    steps.append(f"Deduplicate column '{col_name}' ({order_desc})")
                    transform_count += 1
            else:
                steps.append(f"Deduplicate by composite key ({', '.join(effective_dedup_cols)}) ({order_desc})")
                transform_count += 1
        else:
            steps.append(f"Warning: '{first_by}' is excluded from Dedup Col when ordering by '{first_by}'")

    # Step 9 (CTE step_dedup_full): Full-row deduplication
    if row_dedup_var.get():
        if use_specific_date:
            steps.append(f"Remove duplicate full rows across selected columns excluding '{first_by}' ({order_desc})")
        else:
            steps.append(f"Remove duplicate full rows across selected columns ({order_desc})")
        transform_count += 1

    # Step 10: Final sort & export stage
    steps.append("Restore original row order")

    limit_mode = export_limit_mode_var.get().strip() or "All Rows"
    limit_val = export_limit_val_var.get().strip()
    fmt = out_format_var.get()
    if limit_mode == "First X Rows":
        steps.append(f"Export first {limit_val or 'X'} rows as {fmt}")
        transform_count += 1
    elif limit_mode == "Random Sample (%)":
        steps.append(f"Export ~{limit_val or 'X'}% random sample as {fmt}")
        transform_count += 1
    else:
        steps.append(f"Export all cleaned rows as {fmt}")

    return transform_count, steps


def refresh_steps_window():
    count, steps = build_cleaning_steps_list()
    if count > 0:
        steps_toggle_btn.config(text=f"Cleaning Steps ({count})")
    else:
        steps_toggle_btn.config(text="Cleaning Steps")

    if not steps_window.winfo_exists():
        return

    steps_text.config(state="normal")
    steps_text.delete("1.0", "end")
    if not steps:
        steps_text.insert("end", "Open a Parquet or CSV file to view the ordered cleaning steps.")
    else:
        for idx, desc in enumerate(steps, start=1):
            steps_text.insert("end", f"{idx}. {desc}\n")
    steps_text.config(state="disabled")


def toggle_steps_window():
    global steps_window_visible
    if steps_window_visible and steps_window.winfo_exists():
        hide_steps_window()
    else:
        refresh_steps_window()
        steps_window.deiconify()
        steps_window.lift()
        steps_window_visible = True
        steps_toggle_btn.config(relief="sunken")


def hide_steps_window():
    global steps_window_visible
    if steps_window.winfo_exists():
        steps_window.withdraw()
    steps_window_visible = False
    steps_toggle_btn.config(relief="raised")


# --- DuckDB Cleaning SQL Compiler ---
def build_cleaning_sql(include_order_by=True):
    if not source_relation_sql:
        raise ValueError("Please open a Parquet or CSV file first.")

    # Every global and per-column operation operates strictly on selected ('Keep' checked) columns
    kept_rows = [r for r in row_widget_refs if r["keep_var"].get()]
    if not kept_rows:
        raise ValueError("Check at least one column under 'Keep' to include in the output.")

    all_col_names = [r["col"] for r in kept_rows]
    row_id_alias = quote_ident(make_unique_alias("__duckclean_row_id_9f8e7d", all_col_names))
    dup_comp_alias = quote_ident(make_unique_alias("__duckclean_dup_comp_9f8e7d", all_col_names))
    dup_full_alias = quote_ident(make_unique_alias("__duckclean_dup_full_9f8e7d", all_col_names))

    dedup_whole_rows = row_dedup_var.get()
    dedup_col_names = [r["col"] for r in kept_rows if r["dedup_var"].get()]
    drop_null_rows = [r for r in kept_rows if r["drop_null_var"].get()]
    first_by = first_by_var.get().strip() or "By Order"
    dedup_mode = dedup_mode_var.get().strip() or "Composite Key"

    kept_date_cols = [r["col"] for r in kept_rows if r["category"] == "temporal"]
    use_specific_date = first_by != "By Order" and first_by in kept_date_cols

    if use_specific_date:
        order_clause = f"{quote_ident(first_by)} ASC NULLS LAST, {row_id_alias} ASC"
    else:
        order_clause = f"{row_id_alias} ASC"

    # Exclude the specific chosen date column from duplicate comparison keys when deduplicating by it
    if dedup_col_names:
        if use_specific_date:
            effective_dedup_cols = [c for c in dedup_col_names if c != first_by]
            if not effective_dedup_cols:
                raise ValueError(
                    f"'{first_by}' is excluded from duplicate rules when deduplicating by '{first_by}'. "
                    "Select at least one other column for Dedup Col."
                )
        else:
            effective_dedup_cols = dedup_col_names
    else:
        effective_dedup_cols = []

    if dedup_whole_rows:
        if use_specific_date:
            non_date_kept_idents = [quote_ident(r["col"]) for r in kept_rows if r["col"] != first_by]
            if not non_date_kept_idents:
                raise ValueError(f"Cannot deduplicate rows ignoring '{first_by}' when no other columns are selected.")
            full_row_partition_list = ", ".join(non_date_kept_idents)
        else:
            full_row_partition_list = ", ".join(quote_ident(r["col"]) for r in kept_rows)
    else:
        full_row_partition_list = ""

    # Track effective category per selected column across sequential CTE stages
    eff_categories = {r["col"]: r["category"] for r in kept_rows}

    null_tokens = parse_null_tokens(null_values_var.get())
    case_insensitive = case_insensitive_var.get()

    kept_idents = [quote_ident(r["col"]) for r in kept_rows]
    kept_select_list = ", ".join(kept_idents)

    # CTE 1: Base stream containing ONLY selected columns
    cte_base = (
        f"base AS (\n"
        f"    SELECT {kept_select_list}, row_number() OVER () AS {row_id_alias}\n"
        f"    FROM {source_relation_sql}\n"
        f")"
    )

    # CTE 2: Whitespace canonicalization (only on selected text columns)
    ws_exprs = []
    for r in kept_rows:
        col = r["col"]
        q_col = quote_ident(col)
        if r["ws_var"].get() and eff_categories[col] == "text":
            norm_lf = f"replace(replace({q_col}, chr(13) || chr(10), chr(10)), chr(13), chr(10))"
            collapse_spaces = f"regexp_replace({norm_lf}, '[ \\t]+', ' ', 'g')"
            trimmed = f"regexp_replace({collapse_spaces}, '^\\s+|\\s+$', '', 'g')"
            ws_exprs.append(f"{trimmed} AS {q_col}")
        else:
            ws_exprs.append(q_col)
    cte_ws = f"step_ws AS (\n    SELECT {', '.join(ws_exprs)}, {row_id_alias}\n    FROM base\n)"

    # CTE 3: Per-column ordered multi-value Find & Replace (only on selected columns)
    replace_exprs = []
    for r in kept_rows:
        col = r["col"]
        q_col = quote_ident(col)
        find_val = r["find_var"].get()
        repl_val = r["replace_var"].get()
        pairs = parse_find_replace_pairs(col, find_val, repl_val)
        if pairs:
            cat = eff_categories[col]
            if cat == "text":
                expr = q_col
                for f_tok, r_tok in pairs:
                    expr = f"replace({expr}, {quote_literal(f_tok)}, {quote_literal(r_tok)})"
                replace_exprs.append(f"{expr} AS {q_col}")
            elif cat in ("integer", "float", "temporal", "boolean"):
                expr = f"CAST({q_col} AS VARCHAR)"
                for f_tok, r_tok in pairs:
                    expr = f"replace({expr}, {quote_literal(f_tok)}, {quote_literal(r_tok)})"
                replace_exprs.append(
                    f"CASE WHEN {q_col} IS NULL THEN NULL ELSE {expr} END AS {q_col}"
                )
                eff_categories[col] = "text"
            else:
                raise ValueError(
                    f"Text replacement is not supported on column '{col}' ({r['display_type']})."
                )
        else:
            replace_exprs.append(q_col)
    cte_replace = f"step_replace AS (\n    SELECT {', '.join(replace_exprs)}, {row_id_alias}\n    FROM step_ws\n)"

    # CTE 4: Lowercase / Uppercase conversion (only on selected text columns)
    case_exprs = []
    for r in kept_rows:
        col = r["col"]
        q_col = quote_ident(col)
        cat = eff_categories[col]
        if cat == "text" and r["lower_var"].get():
            case_exprs.append(f"lower({q_col}) AS {q_col}")
        elif cat == "text" and r["upper_var"].get():
            case_exprs.append(f"upper({q_col}) AS {q_col}")
        else:
            case_exprs.append(q_col)
    cte_case = f"step_case AS (\n    SELECT {', '.join(case_exprs)}, {row_id_alias}\n    FROM step_replace\n)"

    # CTE 5: Null-value string / numeric / NaN mapping -> NULL (only on selected columns)
    null_exprs = []
    if null_tokens:
        if case_insensitive:
            lowered_literals = ", ".join(quote_literal(s.lower()) for s in null_tokens)
        else:
            exact_literals = ", ".join(quote_literal(s) for s in null_tokens)

    lowered_token_set = {t.strip().lower() for t in null_tokens}

    for r in kept_rows:
        col = r["col"]
        q_col = quote_ident(col)
        cat = eff_categories[col]
        if r["null_var"].get():
            if cat == "float":
                conds = [f"isnan({q_col})"]
                if any(tok in lowered_token_set for tok in ("inf", "+inf", "infinity", "+infinity")):
                    conds.append(f"(isinf({q_col}) AND {q_col} > 0)")
                if any(tok in lowered_token_set for tok in ("-inf", "-infinity")):
                    conds.append(f"(isinf({q_col}) AND {q_col} < 0)")
                num_literals = extract_numeric_null_literals(null_tokens, "float", r["duck_type"])
                if num_literals:
                    conds.append(f"{q_col} IN ({', '.join(num_literals)})")
                null_exprs.append(f"CASE WHEN {' OR '.join(conds)} THEN NULL ELSE {q_col} END AS {q_col}")
            elif cat == "integer":
                num_literals = extract_numeric_null_literals(null_tokens, "integer", r["duck_type"])
                if num_literals:
                    null_exprs.append(
                        f"CASE WHEN {q_col} IN ({', '.join(num_literals)}) THEN NULL ELSE {q_col} END AS {q_col}"
                    )
                else:
                    null_exprs.append(q_col)
            elif cat == "text" and null_tokens:
                if case_insensitive:
                    null_exprs.append(
                        f"CASE WHEN {q_col} IS NULL OR lower({q_col}) IN ({lowered_literals}) THEN NULL ELSE {q_col} END AS {q_col}"
                    )
                else:
                    null_exprs.append(
                        f"CASE WHEN {q_col} IS NULL OR {q_col} IN ({exact_literals}) THEN NULL ELSE {q_col} END AS {q_col}"
                    )
            else:
                null_exprs.append(q_col)
        else:
            null_exprs.append(q_col)
    cte_null = f"step_null AS (\n    SELECT {', '.join(null_exprs)}, {row_id_alias}\n    FROM step_case\n)"

    # CTE 6: Empty list/map or blank string collapsing -> NULL (only on selected columns)
    empty_exprs = []
    for r in kept_rows:
        col = r["col"]
        q_col = quote_ident(col)
        cat = eff_categories[col]
        if r["empty_var"].get():
            if cat == "list_or_map":
                empty_exprs.append(f"CASE WHEN {q_col} IS NULL OR len({q_col}) = 0 THEN NULL ELSE {q_col} END AS {q_col}")
            elif cat == "text":
                empty_exprs.append(
                    f"CASE WHEN {q_col} IS NULL OR regexp_matches({q_col}, '^(\\s*|\\[\\s*\\]|\\{{\\s*\\}})$') THEN NULL ELSE {q_col} END AS {q_col}"
                )
            else:
                empty_exprs.append(q_col)
        else:
            empty_exprs.append(q_col)
    cte_empty = f"step_empty AS (\n    SELECT {', '.join(empty_exprs)}, {row_id_alias}\n    FROM step_null\n)"

    ctes = [cte_base, cte_ws, cte_replace, cte_case, cte_null, cte_empty]
    current_cte = "step_empty"

    # CTE 7: Drop rows if any selected column with 'Drop if NULL' contains NULL
    if drop_null_rows:
        not_null_conds = [f"{quote_ident(r['col'])} IS NOT NULL" for r in drop_null_rows]
        ctes.append(
            f"step_filtered AS (\n"
            f"    SELECT {kept_select_list}, {row_id_alias}\n"
            f"    FROM {current_cte}\n"
            f"    WHERE {' AND '.join(not_null_conds)}\n"
            f")"
        )
        current_cte = "step_filtered"

    # CTE 8a: Column deduplication (Composite Key OR Sequential Per-Column passes on selected columns)
    if effective_dedup_cols:
        if dedup_mode == "Per Column (Sequential)":
            for idx, col_name in enumerate(effective_dedup_cols):
                q_col = quote_ident(col_name)
                step_alias = quote_ident(make_unique_alias(f"__duckclean_dup_col_{idx}_9f8e7d", all_col_names))
                ranked_cte = f"step_dedup_col_{idx}"
                kept_cte = f"step_dedup_col_{idx}_kept"
                ctes.append(
                    f"{ranked_cte} AS (\n"
                    f"    SELECT {kept_select_list}, {row_id_alias},\n"
                    f"           row_number() OVER (PARTITION BY {q_col} ORDER BY {order_clause}) AS {step_alias}\n"
                    f"    FROM {current_cte}\n"
                    f")"
                )
                ctes.append(
                    f"{kept_cte} AS (\n"
                    f"    SELECT {kept_select_list}, {row_id_alias}\n"
                    f"    FROM {ranked_cte}\n"
                    f"    WHERE {step_alias} = 1\n"
                    f")"
                )
                current_cte = kept_cte
        else:
            dedup_key_list = ", ".join(quote_ident(c) for c in effective_dedup_cols)
            ctes.append(
                f"step_dedup_cols AS (\n"
                f"    SELECT {kept_select_list}, {row_id_alias},\n"
                f"           row_number() OVER (PARTITION BY {dedup_key_list} ORDER BY {order_clause}) AS {dup_comp_alias}\n"
                f"    FROM {current_cte}\n"
                f")"
            )
            ctes.append(
                f"step_dedup_cols_kept AS (\n"
                f"    SELECT {kept_select_list}, {row_id_alias}\n"
                f"    FROM step_dedup_cols\n"
                f"    WHERE {dup_comp_alias} = 1\n"
                f")"
            )
            current_cte = "step_dedup_cols_kept"

    # CTE 8b: Full-row deduplication across selected columns (excluding the chosen date column when ordering by it)
    if dedup_whole_rows:
        ctes.append(
            f"step_dedup_full AS (\n"
            f"    SELECT {kept_select_list}, {row_id_alias},\n"
            f"           row_number() OVER (PARTITION BY {full_row_partition_list} ORDER BY {order_clause}) AS {dup_full_alias}\n"
            f"    FROM {current_cte}\n"
            f")"
        )
        ctes.append(
            f"step_dedup_full_kept AS (\n"
            f"    SELECT {kept_select_list}, {row_id_alias}\n"
            f"    FROM step_dedup_full\n"
            f"    WHERE {dup_full_alias} = 1\n"
            f")"
        )
        current_cte = "step_dedup_full_kept"

    order_tail = f"\nORDER BY {row_id_alias} ASC" if include_order_by else ""
    final_select = f"SELECT {kept_select_list}\nFROM {current_cte}{order_tail}"
    return f"WITH\n{',\n'.join(ctes)}\n{final_select}"


def build_null_sum_expr(kept_cols):
    return " + ".join(f"(CASE WHEN {quote_ident(c)} IS NULL THEN 1 ELSE 0 END)" for c in kept_cols)


def run_preview():
    global is_busy
    if is_busy:
        return

    try:
        full_sql = build_cleaning_sql(include_order_by=True)
        kept_cols = [r["col"] for r in row_widget_refs if r["keep_var"].get()]
    except Exception as e:
        messagebox.showwarning("Invalid Cleaning Configuration", str(e))
        return

    refresh_steps_window()
    include_unique = show_unique_var.get()

    is_busy = True
    set_controls_state("disabled")
    upload_btn.config(state="disabled")
    cancel_btn.config(state="normal")
    status_label.config(text="Calculating full dataset stats and generating 500-row preview...", fg="#1f6aa5")

    threading.Thread(
        target=_preview_worker,
        args=(full_sql, kept_cols, include_unique),
        daemon=True,
    ).start()


def _preview_worker(full_sql, kept_cols, include_unique):
    global active_duckdb_con
    con = None
    start = time.perf_counter()
    try:
        con = duckdb.connect(database=":memory:")
        with con_lock:
            active_duckdb_con = con

        # Execute on the same connection so the TEMP TABLE stays visible across statements
        con.execute(f"CREATE TEMP TABLE __duckclean_preview_tmp AS (\n{full_sql}\n)")
        clean_table = fetch_arrow_compat(con, f"SELECT * FROM __duckclean_preview_tmp LIMIT {PREVIEW_ROW_LIMIT}")

        post_desc = con.execute("DESCRIBE __duckclean_preview_tmp").fetchall()
        post_col_cat_pairs = [(d[0], classify_data_type(d[1])) for d in post_desc]
        col_preview_stats = compute_column_preview_stats(
            con,
            "__duckclean_preview_tmp",
            post_col_cat_pairs,
            include_unique=include_unique,
        )

        null_sum_expr = build_null_sum_expr(kept_cols)
        after_rows, null_total = con.execute(
            f"SELECT COUNT(*), COALESCE(SUM({null_sum_expr}), 0) FROM __duckclean_preview_tmp"
        ).fetchall()[0]

        elapsed = time.perf_counter() - start
        root.after(0, _on_preview_done, clean_table, col_preview_stats, after_rows, null_total, elapsed)
    except Exception as e:
        root.after(0, _on_worker_error, str(e))
    finally:
        with con_lock:
            if active_duckdb_con is con:
                active_duckdb_con = None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _on_preview_done(clean_table, col_preview_stats, after_rows, null_total, elapsed):
    global is_busy
    is_busy = False
    set_controls_state("normal")
    upload_btn.config(state="normal")
    cancel_btn.config(state="disabled")

    render_treeview(preview_tree, clean_table, col_preview_stats)

    removed_total = max(0, total_source_rows - after_rows)
    preview_info_label.config(
        text=(
            f"Showing first {PREVIEW_ROW_LIMIT} rows   |   "
            f"Full Dataset — Before: {total_source_rows:,} rows  |  "
            f"After: {after_rows:,} rows ({removed_total:,} removed)  |  "
            f"NULL cells: {null_total:,}"
        ),
        fg="#111111",
    )

    status_label.config(
        text=f"Completed in {format_duration(elapsed)}.",
        fg="#27ae60",
    )


def start_clean_and_export():
    global is_busy
    if is_busy or not selected_source:
        return

    try:
        full_sql = build_cleaning_sql(include_order_by=True)
        kept_cols = [r["col"] for r in row_widget_refs if r["keep_var"].get()]
        limit_mode, limit_val = parse_export_limit()
    except Exception as e:
        messagebox.showwarning("Invalid Cleaning Configuration", str(e))
        return

    refresh_steps_window()

    fmt = out_format_var.get()
    is_parquet = fmt == "Parquet"
    default_ext = ".parquet" if is_parquet else ".csv"
    filetypes = [("Parquet File", "*.parquet")] if is_parquet else [("CSV File", "*.csv")]

    base_name = os.path.splitext(os.path.basename(os.path.normpath(selected_source)))[0]
    if base_name.endswith(".tar") or base_name.endswith(".csv"):
        base_name = os.path.splitext(base_name)[0]

    out_path = filedialog.asksaveasfilename(
        title="Save Cleaned Dataset",
        initialfile=f"{base_name}_cleaned{default_ext}",
        defaultextension=default_ext,
        filetypes=filetypes,
    )
    if not out_path:
        return

    # Prevent overwriting the active input file
    src_real = os.path.realpath(selected_source)
    out_real = os.path.realpath(out_path)
    if out_real == src_real:
        messagebox.showerror(
            "Invalid Destination",
            "Cannot overwrite the active input file.",
        )
        return

    if os.path.isdir(out_path):
        messagebox.showerror("Invalid Destination", "Selected output path is an existing directory.")
        return

    is_busy = True
    set_controls_state("disabled")
    upload_btn.config(state="disabled")
    cancel_btn.config(state="normal")
    status_label.config(text=f"Streaming cleaned data to {os.path.basename(out_path)}...", fg="#1f6aa5")

    threading.Thread(
        target=_export_worker,
        args=(full_sql, kept_cols, out_path, is_parquet, limit_mode, limit_val),
        daemon=True,
    ).start()


def _export_worker(full_sql, kept_cols, out_path, is_parquet, limit_mode, limit_val):
    global active_duckdb_con
    con = None
    tmp_path = None
    start = time.perf_counter()

    try:
        out_dir = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(out_dir, exist_ok=True)
        suffix = ".parquet" if is_parquet else ".csv"
        tmp_fd = tempfile.NamedTemporaryFile(dir=out_dir, prefix=".tmp_duckclean_", suffix=suffix, delete=False)
        tmp_path = tmp_fd.name
        tmp_fd.close()

        norm_tmp = tmp_path.replace("\\", "/")

        con = duckdb.connect(database=":memory:")
        with con_lock:
            active_duckdb_con = con

        if limit_mode == "first_x":
            export_query = f"SELECT * FROM (\n{full_sql}\n) AS __duckclean_sub\nLIMIT {int(limit_val)}"
        elif limit_mode == "random_pct" and limit_val < 100.0:
            prob = float(limit_val) / 100.0
            export_query = f"SELECT * FROM (\n{full_sql}\n) AS __duckclean_sub\nWHERE random() < {prob:.12g}"
        else:
            export_query = full_sql

        # Execute on the same connection so the TEMP TABLE stays visible across statements
        con.execute(f"CREATE TEMP TABLE __duckclean_export_tmp AS (\n{export_query}\n)")
        null_sum_expr = build_null_sum_expr(kept_cols)
        written_rows, written_nulls = con.execute(
            f"SELECT COUNT(*), COALESCE(SUM({null_sum_expr}), 0) FROM __duckclean_export_tmp"
        ).fetchall()[0]

        if is_parquet:
            copy_sql = f"COPY __duckclean_export_tmp TO {quote_literal(norm_tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        else:
            copy_sql = f"COPY __duckclean_export_tmp TO {quote_literal(norm_tmp)} (FORMAT CSV, HEADER TRUE)"

        con.execute(copy_sql)

        # Close DuckDB before replacing the destination file so all file handles are released
        with con_lock:
            if active_duckdb_con is con:
                active_duckdb_con = None
        con.close()
        con = None

        os.replace(tmp_path, out_path)
        tmp_path = None

        elapsed = time.perf_counter() - start
        root.after(0, _on_export_done, out_path, written_rows, written_nulls, elapsed)
    except Exception as e:
        root.after(0, _on_worker_error, str(e))
    finally:
        with con_lock:
            if con is not None and active_duckdb_con is con:
                active_duckdb_con = None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _on_export_done(out_path, written_rows, written_nulls, elapsed):
    global is_busy
    is_busy = False
    set_controls_state("normal")
    upload_btn.config(state="normal")
    cancel_btn.config(state="disabled")

    removed_rows = max(0, total_source_rows - written_rows)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)

    preview_info_label.config(
        text=(
            f"Showing first {PREVIEW_ROW_LIMIT} rows   |   "
            f"Full Dataset — Before: {total_source_rows:,} rows  |  "
            f"After: {written_rows:,} rows ({removed_rows:,} removed)  |  "
            f"NULL cells: {written_nulls:,}"
        ),
        fg="#111111",
    )

    status_label.config(
        text=f"Saved {os.path.basename(out_path)} ({size_mb:.2f} MB) in {format_duration(elapsed)}.",
        fg="#27ae60",
    )


def _on_worker_error(err_msg):
    global is_busy
    is_busy = False
    set_controls_state("normal")
    upload_btn.config(state="normal")
    cancel_btn.config(state="disabled")
    if "interrupt" in err_msg.lower():
        status_label.config(text="Operation cancelled.", fg="#c0392b")
    else:
        status_label.config(text=f"Error: {err_msg}", fg="#c0392b")
        messagebox.showerror("Cleaning Error", err_msg)


def request_cancel():
    with con_lock:
        if active_duckdb_con is not None:
            try:
                active_duckdb_con.interrupt()
            except Exception:
                pass
    cancel_btn.config(state="disabled")
    status_label.config(text="Cancelling DuckDB operation...", fg="#c0392b")


def on_window_close():
    if is_busy:
        if not messagebox.askyesno(
            "Operation Running",
            "A job is currently running. Cancel and close the window?",
        ):
            return
        request_cancel()
        root.after(150, _wait_and_close)
        return
    root.destroy()


def _wait_and_close():
    if is_busy:
        root.after(100, _wait_and_close)
    else:
        root.destroy()


def clear_treeview(tree):
    for item in tree.get_children():
        tree.delete(item)
    tree["columns"] = ()


def rebuild_preview_header_bar():
    """Render column headers directly as lightweight Canvas items for fast horizontal scrolling."""
    preview_header_canvas.delete("all")

    if not latest_preview_cols:
        preview_header_canvas.configure(scrollregion=(0, 0, 0, 26))
        return

    x = 0
    for col in latest_preview_cols:
        stats = latest_preview_col_stats.get(col)

        name_w = preview_header_bold_font.measure(col) + 10
        if stats:
            null_text = f"Null: {stats['null_pct']}"
            blank_text = f"Blank: {stats['blank_pct']}"
            null_bg, null_fg = get_pct_red_colors(stats["null_pct_val"])
            blank_bg, blank_fg = get_pct_red_colors(stats["blank_pct_val"])

            null_w = preview_stat_font.measure(null_text) + 10
            blank_w = preview_stat_font.measure(blank_text) + 10

            uniq_cnt = stats.get("unique_count")
            if uniq_cnt is not None:
                uniq_text = f"Unique: {uniq_cnt:,}"
                uniq_w = preview_stat_font.measure(uniq_text) + 10
                col_width = max(72, name_w + null_w + blank_w + uniq_w + 10)
            else:
                uniq_text = None
                col_width = max(72, name_w + null_w + blank_w + 8)
        else:
            null_text = None
            blank_text = None
            uniq_text = None
            col_width = max(72, name_w + 8)

        preview_header_canvas.create_rectangle(
            x, 0, x + col_width, 26, fill="#f0f0f0", outline="#cccccc", width=1
        )
        preview_header_canvas.create_text(
            x + 5, 13, text=col, font=preview_header_bold_font, fill="#111111", anchor="w"
        )

        if stats:
            cur_x = x + name_w
            preview_header_canvas.create_rectangle(
                cur_x, 4, cur_x + null_w, 22, fill=null_bg, outline=""
            )
            preview_header_canvas.create_text(
                cur_x + null_w / 2.0,
                13,
                text=null_text,
                font=preview_stat_bold_font if stats["null_pct_val"] > 0 else preview_stat_font,
                fill=null_fg,
                anchor="center",
            )
            cur_x += null_w + 2

            preview_header_canvas.create_rectangle(
                cur_x, 4, cur_x + blank_w, 22, fill=blank_bg, outline=""
            )
            preview_header_canvas.create_text(
                cur_x + blank_w / 2.0,
                13,
                text=blank_text,
                font=preview_stat_bold_font if stats["blank_pct_val"] > 0 else preview_stat_font,
                fill=blank_fg,
                anchor="center",
            )
            cur_x += blank_w + 2

            if uniq_text is not None:
                preview_header_canvas.create_text(
                    cur_x + 2,
                    13,
                    text=uniq_text,
                    font=preview_stat_font,
                    fill="#333333",
                    anchor="w",
                )

        preview_tree.column(col, width=col_width, minwidth=col_width, anchor="w", stretch=False)
        x += col_width

    preview_header_canvas.configure(scrollregion=(0, 0, x, 26))
    preview_header_canvas.xview_moveto(0)


def render_treeview(tree, arrow_table, col_preview_stats=None):
    global latest_preview_cols, latest_preview_col_stats

    clear_treeview(tree)
    cols = arrow_table.column_names
    latest_preview_cols = list(cols)
    latest_preview_col_stats = dict(col_preview_stats) if col_preview_stats else {}

    tree["columns"] = cols
    rebuild_preview_header_bar()

    py_cols = [arrow_table.column(i).to_pylist() for i in range(arrow_table.num_columns)]
    for row_vals in zip(*py_cols):
        tree.insert("", "end", values=[format_preview_cell(v) for v in row_vals])


# --- Desktop Window ---
root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
root.title("Data Cleaning Tool")
root.geometry("1460x780")
root.minsize(1240, 600)
root.protocol("WM_DELETE_WINDOW", on_window_close)

preview_header_bold_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")
preview_stat_font = tkfont.Font(family="Segoe UI", size=8, weight="normal")
preview_stat_bold_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")

if HAS_DND:
    root.drop_target_register(DND_FILES)
    root.dnd_bind("<<Drop>>", on_drop)

# Hidden Toggleable Window for Step-by-Step Cleaning Order
steps_window = tk.Toplevel(root)
steps_window.title("Cleaning Steps (Execution Order)")
steps_window.geometry("560x380")
steps_window.minsize(420, 260)
steps_window.protocol("WM_DELETE_WINDOW", hide_steps_window)
steps_window.withdraw()

tk.Label(
    steps_window,
    text="Active Cleaning Pipeline (Exact Execution Order):",
    font=("Segoe UI", 9, "bold"),
    anchor="w",
).pack(fill="x", padx=12, pady=(10, 6))

steps_text_frame = tk.Frame(steps_window)
steps_text_frame.pack(fill="both", expand=True, padx=12, pady=(0, 10))

steps_text = tk.Text(
    steps_text_frame,
    font=("Consolas", 9),
    wrap="word",
    bg="#fafafa",
    fg="#111111",
    padx=8,
    pady=8,
    state="disabled",
)
steps_scroll = ttk.Scrollbar(steps_text_frame, orient="vertical", command=steps_text.yview)
steps_text.configure(yscrollcommand=steps_scroll.set)
steps_text.pack(side="left", fill="both", expand=True)
steps_scroll.pack(side="right", fill="y")

# Top Bar: File Loader + Export Controls
top_frame = tk.Frame(root)
top_frame.pack(fill="x", padx=16, pady=(12, 8))

upload_btn = tk.Button(top_frame, text="Open File", font=("Segoe UI", 10, "bold"), width=11, command=upload_file)
upload_btn.pack(side="left")

hint_text = (
    "Drag & drop or open a .parquet or .csv file"
    if HAS_DND
    else "Open a .parquet or .csv file"
)
file_label = tk.Label(top_frame, text=hint_text, font=("Segoe UI", 10), fg="#555555")
file_label.pack(side="left", padx=12)

export_btn = tk.Button(
    top_frame,
    text="Clean & Export...",
    font=("Segoe UI", 10, "bold"),
    bg="#1f6aa5",
    fg="white",
    width=16,
    state="disabled",
    command=start_clean_and_export,
)
export_btn.pack(side="right")

out_format_var = tk.StringVar(value="Parquet")
out_format_cb = ttk.Combobox(
    top_frame,
    textvariable=out_format_var,
    values=OUTPUT_FORMATS,
    state="disabled",
    font=("Segoe UI", 9),
    width=9,
)
out_format_cb.pack(side="right", padx=(4, 8))
tk.Label(top_frame, text="Output:", font=("Segoe UI", 9, "bold")).pack(side="right")

export_limit_val_var = tk.StringVar(value="")
export_limit_val_entry = tk.Entry(
    top_frame,
    textvariable=export_limit_val_var,
    font=("Consolas", 9),
    width=8,
    state="disabled",
)
export_limit_val_entry.pack(side="right", padx=(4, 10))

export_limit_mode_var = tk.StringVar(value="All Rows")
export_limit_mode_cb = ttk.Combobox(
    top_frame,
    textvariable=export_limit_mode_var,
    values=EXPORT_LIMIT_MODES,
    state="disabled",
    font=("Segoe UI", 9),
    width=17,
)
export_limit_mode_cb.bind("<<ComboboxSelected>>", on_export_limit_mode_changed)
export_limit_mode_cb.pack(side="right", padx=(4, 0))
tk.Label(top_frame, text="Export:", font=("Segoe UI", 9, "bold")).pack(side="right")

preview_btn = tk.Button(
    top_frame,
    text="Preview Changes",
    font=("Segoe UI", 9, "bold"),
    width=15,
    state="disabled",
    command=run_preview,
)
preview_btn.pack(side="right", padx=(0, 12))

steps_toggle_btn = tk.Button(
    top_frame,
    text="Cleaning Steps",
    font=("Segoe UI", 9),
    width=16,
    command=toggle_steps_window,
)
steps_toggle_btn.pack(side="right", padx=(0, 8))

cancel_btn = tk.Button(
    top_frame,
    text="Cancel",
    font=("Segoe UI", 9),
    width=8,
    state="disabled",
    command=request_cancel,
)
cancel_btn.pack(side="right", padx=(0, 6))

# Global Rules Bar (Null tokens + Preview Stats + Deduplication)
rules_bar = tk.LabelFrame(root, text="Global Rules", font=("Segoe UI", 9, "bold"), padx=10, pady=6)
rules_bar.pack(fill="x", padx=16, pady=(0, 6))

row1 = tk.Frame(rules_bar)
row1.pack(fill="x", pady=(0, 4))

tk.Label(row1, text="Null Values -> NULL:", font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 6))
null_values_var = tk.StringVar(value=DEFAULT_NULL_VALUES_TEXT)
null_values_entry = tk.Entry(row1, textvariable=null_values_var, font=("Consolas", 9), width=42, state="disabled")
null_values_entry.pack(side="left", padx=(0, 8))

case_insensitive_var = tk.BooleanVar(value=False)
case_insensitive_chk = tk.Checkbutton(
    row1, text="Case-insensitive", variable=case_insensitive_var, font=("Segoe UI", 8), state="disabled"
)
case_insensitive_chk.pack(side="left", padx=(0, 18))

show_unique_var = tk.BooleanVar(value=False)
show_unique_chk = tk.Checkbutton(
    row1,
    text="Show unique values per column in preview",
    variable=show_unique_var,
    font=("Segoe UI", 8, "bold"),
    state="disabled",
)
show_unique_chk.pack(side="left")

row2 = tk.Frame(rules_bar)
row2.pack(fill="x")

row_dedup_var = tk.BooleanVar(value=False)
row_dedup_chk = tk.Checkbutton(
    row2,
    text="Remove duplicate rows",
    variable=row_dedup_var,
    font=("Segoe UI", 8, "bold"),
    state="disabled",
)
row_dedup_chk.pack(side="left", padx=(0, 16))

tk.Label(row2, text="Deduplicate keeping first by:", font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 6))
first_by_var = tk.StringVar(value="By Order")
first_by_cb = ttk.Combobox(
    row2,
    textvariable=first_by_var,
    values=["By Order"],
    state="disabled",
    font=("Segoe UI", 8),
    width=24,
)
first_by_cb.pack(side="left", padx=(0, 16))

tk.Label(row2, text="Column dedup mode:", font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 6))
dedup_mode_var = tk.StringVar(value="Composite Key")
dedup_mode_cb = ttk.Combobox(
    row2,
    textvariable=dedup_mode_var,
    values=DEDUP_MODES,
    state="disabled",
    font=("Segoe UI", 8),
    width=24,
)
dedup_mode_cb.pack(side="left")

# Live-update the hidden Cleaning Steps window whenever global settings change
for _tracked_var in (
    null_values_var,
    case_insensitive_var,
    row_dedup_var,
    first_by_var,
    dedup_mode_var,
    out_format_var,
    export_limit_val_var,
):
    _tracked_var.trace_add("write", lambda *_args: refresh_steps_window())

# Split Pane: Column Rules Table (top) + Single Preview Output (bottom)
main_paned = ttk.PanedWindow(root, orient="vertical")
main_paned.pack(fill="both", expand=True, padx=16, pady=4)

table_container = tk.Frame(main_paned, bd=1, relief="sunken")
main_paned.add(table_container, weight=3)

canvas = tk.Canvas(table_container, highlightthickness=0)
scrollbar = ttk.Scrollbar(table_container, orient="vertical", command=canvas.yview)
rows_frame = tk.Frame(canvas)

rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
canvas.create_window((0, 0), window=rows_frame, anchor="nw")
canvas.configure(yscrollcommand=scrollbar.set)

canvas.pack(side="left", fill="both", expand=True, padx=8, pady=8)
scrollbar.pack(side="right", fill="y")


def _on_rules_mousewheel(event):
    if event.num == 4:
        canvas.yview_scroll(-1, "units")
    elif event.num == 5:
        canvas.yview_scroll(1, "units")
    else:
        step = -1 if event.delta > 0 else 1
        canvas.yview_scroll(step, "units")
    return "break"


attach_scroll_tag(canvas)
attach_scroll_tag(rows_frame)
root.bind_class(SCROLL_BIND_TAG, "<MouseWheel>", _on_rules_mousewheel)
root.bind_class(SCROLL_BIND_TAG, "<Button-4>", _on_rules_mousewheel)
root.bind_class(SCROLL_BIND_TAG, "<Button-5>", _on_rules_mousewheel)

# Bottom Single Preview Pane (with Fast Canvas-Rendered Column Header Bar)
preview_frame = tk.LabelFrame(
    main_paned,
    text="Data Preview",
    font=("Segoe UI", 9, "bold"),
    padx=6,
    pady=4,
)
main_paned.add(preview_frame, weight=2)

preview_info_label = tk.Label(
    preview_frame,
    text=f"Showing first {PREVIEW_ROW_LIMIT} rows   |   Full Dataset — Before: 0 rows  |  After: 0 rows  |  NULL cells: 0",
    font=("Segoe UI", 8, "bold"),
    fg="#555555",
    anchor="w",
)
preview_info_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))

preview_header_canvas = tk.Canvas(preview_frame, height=26, highlightthickness=0, bd=0, bg="#e8e8e8")
preview_header_canvas.grid(row=1, column=0, sticky="ew")

preview_tree = ttk.Treeview(preview_frame, show="")
preview_y = ttk.Scrollbar(preview_frame, orient="vertical", command=preview_tree.yview)
preview_x = ttk.Scrollbar(preview_frame, orient="horizontal")


def _on_preview_xscroll(*args):
    if len(args) >= 3 and args[0] == "scroll" and args[2] == "units":
        step = int(args[1]) * HSCROLL_UNIT_MULTIPLIER
        preview_tree.xview("scroll", step, "units")
    else:
        preview_tree.xview(*args)
    first, last = preview_tree.xview()
    preview_x.set(first, last)
    preview_header_canvas.xview_moveto(first)


def _on_tree_xview_set(first, last):
    preview_x.set(first, last)
    preview_header_canvas.xview_moveto(first)


def _on_preview_shift_mousewheel(event):
    if event.num == 4:
        delta_units = -HSCROLL_UNIT_MULTIPLIER
    elif event.num == 5:
        delta_units = HSCROLL_UNIT_MULTIPLIER
    else:
        direction = -1 if event.delta > 0 else 1
        delta_units = direction * HSCROLL_UNIT_MULTIPLIER
    preview_tree.xview_scroll(delta_units, "units")
    first, last = preview_tree.xview()
    preview_x.set(first, last)
    preview_header_canvas.xview_moveto(first)
    return "break"


preview_x.config(command=_on_preview_xscroll)
preview_tree.configure(yscrollcommand=preview_y.set, xscrollcommand=_on_tree_xview_set)

for _preview_w in (preview_tree, preview_header_canvas):
    _preview_w.bind("<Shift-MouseWheel>", _on_preview_shift_mousewheel)
    _preview_w.bind("<Shift-Button-4>", _on_preview_shift_mousewheel)
    _preview_w.bind("<Shift-Button-5>", _on_preview_shift_mousewheel)

preview_tree.grid(row=2, column=0, sticky="nsew")
preview_y.grid(row=2, column=1, sticky="ns")
preview_x.grid(row=3, column=0, sticky="ew")
preview_frame.grid_rowconfigure(2, weight=1)
preview_frame.grid_columnconfigure(0, weight=1)

# Bottom Status Bar
status_bar = tk.Frame(root)
status_bar.pack(fill="x", padx=16, pady=(4, 10))

status_label = tk.Label(status_bar, text="Open a Parquet or CSV file to configure cleaning rules.", font=("Segoe UI", 9), fg="#555555")
status_label.pack(side="left")

root.mainloop()
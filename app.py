import os
import re
import sqlite3
import tempfile
import textwrap
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from google import genai
from google.genai.errors import ServerError

# ── Config ──────────────────────────────────────────────────────────────────
UPLOAD_DIR = Path(tempfile.gettempdir()) / "bi_assistant_uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_ROWS_FOR_CHART = 500
SQL_RETRY_LIMIT = 2
GEMINI_MODEL = "gemini-2.5-flash"
OVERLOAD_RETRY_LIMIT = 3
OVERLOAD_RETRY_DELAY = 5

CHART_THEME = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(37,43,74,0.6)",
    font=dict(color="#e0e4f0"),
    xaxis=dict(gridcolor="rgba(108,99,255,0.15)", zerolinecolor="rgba(108,99,255,0.2)"),
    yaxis=dict(gridcolor="rgba(108,99,255,0.15)", zerolinecolor="rgba(108,99,255,0.2)"),
    margin=dict(t=48, b=24, l=16, r=16),
    height=420,
)

TYPE_BADGE_COLORS = {
    "int": "#6c63ff", "integer": "#6c63ff", "real": "#6c63ff", "numeric": "#6c63ff",
    "float": "#6c63ff", "double": "#6c63ff", "text": "#2ea8a0", "varchar": "#2ea8a0",
    "blob": "#a07c2e", "boolean": "#c063a0", "date": "#2e7ca0", "datetime": "#2e7ca0",
    "timestamp": "#2e7ca0",
}

# ── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="BI Assistant",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stSidebar"] { border-right: 1px solid rgba(108,99,255,0.2); }

/* ── Stepper ── */
.stepper { display: flex; align-items: center; justify-content: center;
           padding: 32px 0 40px; gap: 0; }
.stepper-step { display: flex; flex-direction: column; align-items: center; position: relative; }
.stepper-circle { width: 56px; height: 56px; border-radius: 50%; display: flex;
                  align-items: center; justify-content: center; font-size: 1.4rem;
                  font-weight: 700; border: 2px solid rgba(108,99,255,0.3);
                  background: rgba(26,31,54,0.9); color: #6070a0; position: relative; z-index: 1; }
.stepper-circle.active { border-color: #6c63ff; background: rgba(108,99,255,0.15);
                          color: #6c63ff; box-shadow: 0 0 0 4px rgba(108,99,255,0.12); }
.stepper-circle.done { border-color: #2ea8a0; background: rgba(46,168,160,0.12); color: #2ea8a0; }
.stepper-label { font-size: 0.72rem; font-weight: 600; margin-top: 10px; color: #6070a0;
                 letter-spacing: 0.03em; white-space: nowrap; }
.stepper-label.active { color: #e0e4f0; }
.stepper-label.done { color: #2ea8a0; }
.stepper-line { width: 120px; height: 2px; background: rgba(108,99,255,0.15);
                margin-bottom: 28px; }
.stepper-line.done { background: rgba(46,168,160,0.4); }

/* ── How-it-works cards ── */
.hiw-card { background: rgba(37,43,74,0.6); border: 1px solid rgba(108,99,255,0.18);
            border-radius: 16px; padding: 28px 20px 24px; text-align: center;
            transition: border-color 0.2s; height: 100%; box-sizing: border-box; }
.hiw-card:hover { border-color: rgba(108,99,255,0.45); }
.hiw-icon { font-size: 3rem; margin-bottom: 14px; }
.hiw-title { font-size: 0.95rem; font-weight: 700; color: #e0e4f0; margin-bottom: 8px; }
.hiw-desc { font-size: 0.78rem; color: #6070a0; line-height: 1.5; }
[data-testid="column"] > div { height: 100%; }
[data-testid="column"] > div > div { height: 100%; }

/* ── Upload area label ── */
.upload-label { font-size: 1rem; font-weight: 600; color: #c0c8e0; margin-bottom: 8px; }
.fmt-badge { display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px;
             border: 1px solid rgba(108,99,255,0.3); border-radius: 6px;
             font-size: 0.72rem; font-weight: 600; color: #8888bb;
             background: rgba(108,99,255,0.06); margin-right: 6px; }

/* ── Metric cards ── */
.metric-card { background: rgba(37,43,74,0.8); border: 1px solid rgba(108,99,255,0.25);
               border-radius: 12px; padding: 20px 24px; text-align: center; }
.metric-card .value { font-size: 2rem; font-weight: 700; color: #6c63ff; }
.metric-card .label { font-size: 0.8rem; color: #a0a8c0; text-transform: uppercase;
                      letter-spacing: 0.08em; margin-top: 4px; }

/* ── Type badges ── */
.type-badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
              font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
              letter-spacing: 0.05em; margin-left: 4px; }

/* ── Insight box ── */
.insight-box { background: rgba(108,99,255,0.08); border-left: 3px solid #6c63ff;
               border-radius: 0 8px 8px 0; padding: 16px 20px; margin-top: 8px; }
</style>
""", unsafe_allow_html=True)

# ── Session state defaults ──────────────────────────────────────────────────
for key, default in {
    "history": [],
    "page": "upload",
    "db_path": None,
    "db_name": None,
    "ai_overview": None,
    "table_stats": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ── Helpers ──────────────────────────────────────────────────────────────────
def render_steps():
    page = st.session_state.page
    pages = ["upload", "overview", "query"]
    steps = [
        ("☁️", "1. Data Connect"),
        ("⚙️", "2. Schema Analysis"),
        ("📊", "3. Insight Dashboard"),
    ]
    current_idx = pages.index(page)
    html = '<div class="stepper">'
    for i, (icon, label) in enumerate(steps):
        if i < current_idx:
            circle_cls, label_cls = "done", "done"
        elif i == current_idx:
            circle_cls, label_cls = "active", "active"
        else:
            circle_cls, label_cls = "", ""
        if i > 0:
            line_cls = "done" if i <= current_idx else ""
            html += f'<div class="stepper-line {line_cls}"></div>'
        html += (
            f'<div class="stepper-step">'
            f'<div class="stepper-circle {circle_cls}">{icon}</div>'
            f'<div class="stepper-label {label_cls}">{label}</div>'
            f'</div>'
        )
    html += '</div>'
    st.markdown(html, unsafe_allow_html=True)


def type_badge(col_type: str) -> str:
    t = col_type.lower().split("(")[0].strip()
    color = TYPE_BADGE_COLORS.get(t, "#4a5070")
    return f'<span class="type-badge" style="background:{color}22;color:{color};border:1px solid {color}55">{col_type or "—"}</span>'


def metric_card(label: str, value: str):
    st.markdown(
        f'<div class="metric-card"><div class="value">{value}</div><div class="label">{label}</div></div>',
        unsafe_allow_html=True,
    )


def get_db_connection():
    db_path = st.session_state.get("db_path")
    if not db_path or not Path(db_path).exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_schema(conn) -> dict[str, list[dict]]:
    if conn is None:
        return {}
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    tables = [row[0] for row in cursor.fetchall()]
    schema: dict[str, list[dict]] = {}
    for table in tables:
        cursor.execute(f"PRAGMA table_info('{table}');")
        cols = [{"name": r[1], "type": r[2]} for r in cursor.fetchall()]
        schema[table] = cols
    return schema


def get_table_stats(conn, schema: dict[str, list[dict]]) -> dict:
    stats = {}
    for table, cols in schema.items():
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM '{table}';")
        row_count = cursor.fetchone()[0]
        preview = pd.read_sql_query(f"SELECT * FROM '{table}' LIMIT 5;", conn)
        stats[table] = {"rows": row_count, "cols": len(cols), "preview": preview}
    return stats


def schema_to_text(schema: dict[str, list[dict]]) -> str:
    lines = []
    for table, cols in schema.items():
        col_str = ", ".join(f"{c['name']} ({c['type']})" for c in cols)
        lines.append(f"  {table}({col_str})")
    return "\n".join(lines)


def get_genai_client() -> genai.Client | None:
    api_key = st.secrets.get("GOOGLE_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def extract_sql(text: str) -> str:
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"(SELECT\s.+?;)", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def generate_content_with_retry(client: genai.Client, prompt: str) -> str:
    for attempt in range(1, OVERLOAD_RETRY_LIMIT + 1):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            return response.text
        except ServerError:
            if attempt == OVERLOAD_RETRY_LIMIT:
                raise
            time.sleep(OVERLOAD_RETRY_DELAY)


def get_sample_rows(schema: dict[str, list[dict]], conn, max_tables: int = 10) -> str:
    lines = []
    for table in list(schema.keys())[:max_tables]:
        preview = pd.read_sql_query(f"SELECT * FROM '{table}' LIMIT 3;", conn)
        if not preview.empty:
            lines.append(f"  {table} (sample):")
            lines.append(preview.to_string(index=False))
    return "\n".join(lines)


def generate_sql(client: genai.Client, question: str, schema_text: str, sample_text: str, error: str = "") -> str:
    retry_note = ""
    if error:
        retry_note = f"\nThe previous attempt produced this SQL error: {error}\nPlease fix it."

    prompt = textwrap.dedent(f"""
        You are an expert SQL analyst working with a SQLite database.

        DATABASE SCHEMA:
        {schema_text}

        SAMPLE DATA (first 3 rows per table — use this to understand actual data formats,
        especially date/time formats, before writing date functions):
        {sample_text}

        TASK: Write a single, valid SQLite query that answers the following question.
        Return ONLY the SQL query inside a ```sql ... ``` code block — no explanation.
        Use only tables and columns that exist in the schema above.
        Do not use window functions unsupported by SQLite < 3.25.
        IMPORTANT: Always JOIN to resolve IDs into human-readable names when possible.
        Prefer showing descriptive columns (names, categories, labels) over raw ID hashes.
        IMPORTANT: Check the sample data for date formats. If dates are NOT in ISO format
        (YYYY-MM-DD), use SUBSTR and string functions instead of STRFTIME to parse them.
        {retry_note}

        QUESTION: {question}
    """).strip()

    return extract_sql(generate_content_with_retry(client, prompt))


def run_sql(sql: str) -> pd.DataFrame:
    conn = get_db_connection()
    return pd.read_sql_query(sql, conn)


def generate_insight(client: genai.Client, question: str, df: pd.DataFrame) -> str:
    preview = df.head(20).to_markdown(index=False)
    prompt = textwrap.dedent(f"""
        You are a business analyst summarizing query results for a non-technical stakeholder.
        Write exactly 2-3 sentences highlighting the key finding, any notable trend or outlier,
        and a brief actionable takeaway. Be concise and confident — no hedging.

        ORIGINAL QUESTION: {question}

        QUERY RESULTS (up to 20 rows):
        {preview}
    """).strip()

    return generate_content_with_retry(client, prompt).strip()


def generate_followups(client: genai.Client, question: str, schema_text: str) -> list[str]:
    prompt = textwrap.dedent(f"""
        A user asked: "{question}"

        Given this database schema:
        {schema_text}

        Suggest exactly 3 short follow-up questions they might want to ask next.
        Return only a numbered list, one question per line, no explanation.
    """).strip()
    raw = generate_content_with_retry(client, prompt)
    lines = [re.sub(r"^\d+[\.\)]\s*", "", l).strip() for l in raw.strip().splitlines() if l.strip()]
    return lines[:3]


def generate_ai_overview(client: genai.Client, schema_text: str, table_stats: dict) -> str:
    stats_summary = []
    for table, info in table_stats.items():
        stats_summary.append(f"  - {table}: {info['rows']:,} rows, {info['cols']} columns")
    stats_text = "\n".join(stats_summary)

    prompt = textwrap.dedent(f"""
        You are a data analyst who has just received a new database to explore.
        Provide a concise overview of this dataset for a non-technical user.

        DATABASE SCHEMA:
        {schema_text}

        TABLE SIZES:
        {stats_text}

        Write 3-5 paragraphs covering:
        1. What this dataset appears to be about (domain, subject matter).
        2. How the tables relate to each other (key relationships you can infer).
        3. The scale of the data (total records, notable size differences between tables).
        4. Suggested areas to explore — what interesting questions could be answered with this data?

        Be confident and specific. Use plain language.
    """).strip()

    return generate_content_with_retry(client, prompt).strip()


def auto_chart(df: pd.DataFrame, question: str) -> None:
    if df.empty or len(df.columns) < 2:
        st.dataframe(df, use_container_width=True)
        return

    cols = list(df.columns)
    num_cols = df.select_dtypes(include="number").columns.tolist()
    cat_cols = [c for c in cols if c not in num_cols]

    q_lower = question.lower()
    is_time = any(kw in q_lower for kw in ["month", "year", "date", "time", "trend", "over time", "daily", "weekly"])

    color_seq = ["#6c63ff", "#a78bfa", "#2ea8a0", "#f59e0b", "#f472b6"]

    if is_time and len(num_cols) >= 1 and len(cat_cols) >= 1:
        fig = px.line(df, x=cat_cols[0], y=num_cols[0], markers=True,
                      title="Trend Over Time", color_discrete_sequence=color_seq)
    elif len(cat_cols) >= 1 and len(num_cols) >= 1:
        if df[cat_cols[0]].nunique() > 12:
            fig = px.bar(df, x=num_cols[0], y=cat_cols[0], orientation="h",
                         title="Results", color_discrete_sequence=color_seq)
        else:
            fig = px.bar(df, x=cat_cols[0], y=num_cols[0],
                         title="Results", color_discrete_sequence=color_seq)
    elif len(num_cols) >= 2:
        fig = px.scatter(df, x=num_cols[0], y=num_cols[1],
                         title="Scatter", color_discrete_sequence=color_seq)
    else:
        st.dataframe(df, use_container_width=True)
        return

    fig.update_layout(**CHART_THEME)
    st.plotly_chart(fig, use_container_width=True)


def ingest_uploaded_files(uploaded_files) -> str | None:
    sqlite_files = [f for f in uploaded_files if f.name.endswith(".sqlite") or f.name.endswith(".db")]
    csv_files = [f for f in uploaded_files if f.name.endswith(".csv")]

    if sqlite_files:
        db_file = sqlite_files[0]
        db_path = UPLOAD_DIR / db_file.name
        db_path.write_bytes(db_file.getvalue())
        return str(db_path)

    if csv_files:
        db_path = UPLOAD_DIR / "uploaded_data.sqlite"
        conn = sqlite3.connect(str(db_path))
        for csv_file in csv_files:
            table_name = Path(csv_file.name).stem
            table_name = re.sub(r"[^a-zA-Z0-9_]", "_", table_name)
            df = pd.read_csv(csv_file)
            df.to_sql(table_name, conn, if_exists="replace", index=False)
        conn.close()
        return str(db_path)

    return None


# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="padding: 8px 0 20px;">
        <div style="font-size:1.4rem;font-weight:800;background:linear-gradient(135deg,#6c63ff,#a78bfa);
                    -webkit-background-clip:text;-webkit-text-fill-color:transparent;">
            📊 BI Assistant
        </div>
        <div style="font-size:0.75rem;color:#6070a0;margin-top:2px;">Powered by Gemini AI</div>
    </div>
    """, unsafe_allow_html=True)

    if st.session_state.page != "upload":
        if st.button("📂 Upload New Data", use_container_width=True):
            st.session_state.page = "upload"
            st.session_state.db_path = None
            st.session_state.db_name = None
            st.session_state.ai_overview = None
            st.session_state.table_stats = None
            st.session_state.history = []
            st.rerun()

        conn = get_db_connection()
        if conn:
            schema = get_schema(conn)
            if schema:
                st.markdown(f"""
                <div style="background:rgba(46,168,160,0.1);border:1px solid rgba(46,168,160,0.3);
                            border-radius:8px;padding:10px 14px;margin:12px 0;font-size:0.82rem;">
                    ✅ <b>{st.session_state.db_name}</b><br>
                    <span style="color:#6070a0">{len(schema)} tables loaded</span>
                </div>
                """, unsafe_allow_html=True)
                st.markdown("**🗂 Schema**")
                for table, cols in schema.items():
                    with st.expander(f"**{table}** ({len(cols)} cols)"):
                        for c in cols:
                            st.markdown(
                                f'<div style="padding:2px 0;font-size:0.82rem">'
                                f'<code>{c["name"]}</code>{type_badge(c["type"])}</div>',
                                unsafe_allow_html=True,
                            )

    if st.session_state.page == "query" and st.session_state.history:
        st.divider()
        st.markdown("**🕒 Query History**")
        for i, entry in enumerate(reversed(st.session_state.history[-10:])):
            with st.expander(f"Q{len(st.session_state.history) - i}: {entry['question'][:45]}…"):
                st.code(entry["sql"], language="sql")
                if entry.get("insight"):
                    st.caption(entry["insight"])


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: Upload / Welcome
# ═══════════════════════════════════════════════════════════════════════════
if st.session_state.page == "upload":
    render_steps()

    # How it works
    st.markdown("### How it works")
    hiw_cols = st.columns(3)
    for col, icon, title, desc in zip(
        hiw_cols,
        ["🗄️", "🔬", "💬"],
        ["Unified Data Connection", "Intelligent Schema Mapping", "Natural Language Querying"],
        [
            "Import CSV, SQLite, and external DB sources effortlessly",
            "AI automatically detects tables, relationships, and suggests key metrics",
            'Ask questions like "Show me sales by region" and get answers',
        ],
    ):
        col.markdown(
            f'<div class="hiw-card">'
            f'<div class="hiw-icon">{icon}</div>'
            f'<div class="hiw-title">{title}</div>'
            f'<div class="hiw-desc">{desc}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # Upload section
    st.markdown('<div class="upload-label">Drop your data file here</div>', unsafe_allow_html=True)

    uploaded = st.file_uploader(
        "",
        type=["sqlite", "db", "csv"],
        accept_multiple_files=True,
        help="Upload a .sqlite/.db file, or multiple .csv files (each becomes a table).",
        label_visibility="collapsed",
    )

    st.markdown(
        '<div style="margin-top:10px">'
        '<span class="fmt-badge">📄 CSV</span>'
        '<span class="fmt-badge">🗃 SQLITE</span>'
        '<span class="fmt-badge">💾 DB</span>'
        '</div>'
        '<div style="font-size:0.72rem;color:#4a5070;margin-top:6px">• 200MB per file</div>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)

    demo_path = Path(__file__).parent / "data" / "olist.sqlite"
    col1, col2 = st.columns(2)

    with col1:
        upload_btn = st.button(
            "☁️ Upload & Analyze",
            type="primary",
            disabled=not uploaded,
            use_container_width=True,
        )

    with col2:
        demo_available = demo_path.exists()
        demo_btn = st.button(
            "📦 Use Demo Dataset",
            disabled=not demo_available,
            use_container_width=True,
            help="Use the bundled Olist e-commerce dataset." if demo_available else "Place olist.sqlite in data/ to enable.",
        )

    if upload_btn and uploaded:
        with st.spinner("Processing uploaded files…"):
            db_path = ingest_uploaded_files(uploaded)
        if db_path:
            names = ", ".join(f.name for f in uploaded)
            st.session_state.db_path = db_path
            st.session_state.db_name = names
            st.session_state.ai_overview = None
            st.session_state.table_stats = None
            st.session_state.page = "overview"
            st.toast("Data loaded successfully!", icon="✅")
            st.rerun()
        else:
            st.error("Unsupported file type. Please upload .sqlite, .db, or .csv files.")

    if demo_btn:
        st.session_state.db_path = str(demo_path)
        st.session_state.db_name = "Olist E-Commerce (demo)"
        st.session_state.ai_overview = None
        st.session_state.table_stats = None
        st.session_state.page = "overview"
        st.toast("Demo dataset loaded!", icon="📦")
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: Data Overview
# ═══════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "overview":
    render_steps()

    st.markdown(f"## Data Overview")
    st.markdown(f'<div style="color:#6070a0;margin-bottom:24px">Dataset: <b style="color:#a0a8c0">{st.session_state.db_name}</b></div>', unsafe_allow_html=True)

    conn = get_db_connection()
    if conn is None:
        st.error("Could not connect to the database.")
        st.stop()

    schema = get_schema(conn)
    if not schema:
        st.error("No tables found in the database.")
        st.stop()

    if st.session_state.table_stats is None:
        st.session_state.table_stats = get_table_stats(conn, schema)
    table_stats = st.session_state.table_stats

    total_rows = sum(info["rows"] for info in table_stats.values())
    total_tables = len(schema)
    total_cols = sum(info["cols"] for info in table_stats.values())

    m1, m2, m3 = st.columns(3)
    with m1:
        metric_card("Tables", str(total_tables))
    with m2:
        metric_card("Total Columns", str(total_cols))
    with m3:
        metric_card("Total Rows", f"{total_rows:,}")

    st.divider()

    st.markdown("#### 🗂 Tables")
    for table, cols in schema.items():
        info = table_stats[table]
        with st.expander(f"**{table}** — {info['rows']:,} rows · {info['cols']} columns"):
            badge_row = " ".join(
                f'<code style="font-size:0.8rem">{c["name"]}</code>{type_badge(c["type"])}'
                for c in cols
            )
            st.markdown(f'<div style="line-height:2.2;margin-bottom:10px">{badge_row}</div>', unsafe_allow_html=True)
            st.caption("Preview (first 5 rows):")
            st.dataframe(info["preview"], use_container_width=True, hide_index=True)

    st.divider()

    if st.session_state.ai_overview:
        st.markdown("#### 🧠 AI Overview")
        st.markdown(st.session_state.ai_overview)
    else:
        if st.button("🧠 Generate AI Overview", type="secondary", use_container_width=True):
            client = get_genai_client()
            if client is None:
                st.error("Could not initialize AI client. Check that GOOGLE_API_KEY is set in Streamlit secrets.")
            else:
                with st.spinner("Analyzing your data with AI…"):
                    schema_text = schema_to_text(schema)
                    overview = generate_ai_overview(client, schema_text, table_stats)
                    st.session_state.ai_overview = overview
                st.toast("AI overview ready!", icon="🧠")
                st.rerun()

    st.divider()

    if st.button("🔍 Start Querying →", type="primary", use_container_width=True):
        st.session_state.page = "query"
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: Query Interface
# ═══════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "query":
    render_steps()

    st.markdown("## Ask a Question")
    st.markdown(f'<div style="color:#6070a0;margin-bottom:24px">Dataset: <b style="color:#a0a8c0">{st.session_state.db_name}</b></div>', unsafe_allow_html=True)

    conn = get_db_connection()
    if conn is None:
        st.error("Database not connected. Please upload data first.")
        if st.button("Go to Upload"):
            st.session_state.page = "upload"
            st.rerun()
        st.stop()

    schema = get_schema(conn)

    with st.sidebar:
        st.divider()
        st.markdown("**💡 Example Questions**")
        examples = [
            "What are the top 5 categories by total revenue?",
            "How many records are in each table?",
            "What is the distribution of values in the largest table?",
            "Show me monthly trends over time.",
        ]
        for ex in examples:
            if st.button(ex, key=f"ex_{ex[:30]}", use_container_width=True):
                st.session_state["current_question"] = ex
                st.rerun()

    # Apply any prefill into the persistent question key, then clear it
    if "prefill_question" in st.session_state:
        st.session_state["current_question"] = st.session_state.pop("prefill_question")

    # Show suggested follow-ups from last query as quick-select chips
    if st.session_state.get("followups"):
        st.markdown('<div style="font-size:0.8rem;color:#6070a0;margin-bottom:6px">Suggested follow-ups:</div>', unsafe_allow_html=True)
        fu_cols = st.columns(len(st.session_state.followups))
        for col, fq in zip(fu_cols, st.session_state.followups):
            if col.button(fq, use_container_width=True, key=f"fu_{fq[:30]}"):
                st.session_state["current_question"] = fq
                st.session_state["followups"] = []
                st.rerun()

    question = st.text_area(
        "Ask a business question:",
        placeholder="e.g. What are the top 5 categories by revenue?",
        height=90,
        key="current_question",
    )

    run_btn = st.button("🔍 Run Query", type="primary", disabled=not question.strip())

    if run_btn and question.strip():
        client = get_genai_client()
        if client is None:
            st.error("Could not initialize AI client. Check that GOOGLE_API_KEY is set in Streamlit secrets.")
            st.stop()

        schema_text = schema_to_text(schema)
        sample_text = get_sample_rows(schema, conn)

        with st.status("Generating SQL…", expanded=True) as status:
            sql = ""
            df = pd.DataFrame()
            last_error = ""

            for attempt in range(1, SQL_RETRY_LIMIT + 2):
                try:
                    st.write(f"Attempt {attempt}: asking Gemini for SQL…")
                    sql = generate_sql(client, question, schema_text, sample_text, error=last_error)
                    st.write("Executing query…")
                    df = run_sql(sql)
                    last_error = ""
                    break
                except Exception as exc:
                    last_error = str(exc)
                    if attempt > SQL_RETRY_LIMIT:
                        status.update(label="❌ Query failed", state="error")
                        st.error(f"Could not produce valid SQL after {SQL_RETRY_LIMIT + 1} attempts.\n\n**Last error:** {last_error}")
                        if sql:
                            st.code(sql, language="sql")
                        st.stop()
                    st.warning(f"SQL error (retrying): {last_error}")

            st.write("Generating insight and follow-up questions…")
            insight = generate_insight(client, question, df)
            followups = generate_followups(client, question, schema_text)
            status.update(label="✅ Done", state="complete", expanded=False)

        st.toast("Query complete!", icon="✅")

        st.markdown("#### 🔎 Generated SQL")
        st.code(sql, language="sql")

        if df.empty:
            st.markdown("""
            <div style="text-align:center;padding:48px 0;color:#6070a0;">
                <div style="font-size:2.5rem">🔍</div>
                <div style="font-size:1.1rem;margin-top:12px;font-weight:600">No results found</div>
                <div style="font-size:0.85rem;margin-top:6px">Try rephrasing your question or check the schema.</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            col_chart, col_table = st.columns([3, 2])

            with col_chart:
                st.markdown("#### 📈 Chart")
                if len(df) > MAX_ROWS_FOR_CHART:
                    st.caption(f"Showing chart for first {MAX_ROWS_FOR_CHART} rows.")
                    auto_chart(df.head(MAX_ROWS_FOR_CHART), question)
                else:
                    auto_chart(df, question)

            with col_table:
                st.markdown("#### 📋 Data")
                st.dataframe(df, use_container_width=True, height=380)
                csv = df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "⬇️ Export CSV",
                    data=csv,
                    file_name="query_results.csv",
                    mime="text/csv",
                )

        st.markdown("#### 🧠 AI Insight")
        st.markdown(f'<div class="insight-box">{insight}</div>', unsafe_allow_html=True)

        st.session_state.followups = followups
        st.session_state.history.append(
            {"question": question, "sql": sql, "insight": insight}
        )

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

# ── Page setup ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="BI Assistant",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state defaults ──────────────────────────────────────────────────
for key, default in {
    "history": [],
    "page": "upload",       # upload | overview | query
    "db_path": None,        # path to active sqlite file
    "db_name": None,        # display name
    "ai_overview": None,    # cached AI overview text
    "table_stats": None,    # cached {table: {rows, cols, preview}}
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ── Helpers ──────────────────────────────────────────────────────────────────
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

    if is_time and len(num_cols) >= 1 and len(cat_cols) >= 1:
        fig = px.line(df, x=cat_cols[0], y=num_cols[0], markers=True, title="Trend Over Time")
    elif len(cat_cols) >= 1 and len(num_cols) >= 1:
        if df[cat_cols[0]].nunique() > 12:
            fig = px.bar(df, x=num_cols[0], y=cat_cols[0], orientation="h", title="Results")
        else:
            fig = px.bar(df, x=cat_cols[0], y=num_cols[0], title="Results")
    elif len(num_cols) >= 2:
        fig = px.scatter(df, x=num_cols[0], y=num_cols[1], title="Scatter")
    else:
        st.dataframe(df, use_container_width=True)
        return

    fig.update_layout(margin=dict(t=40, b=20), height=420)
    st.plotly_chart(fig, use_container_width=True)


def ingest_uploaded_files(uploaded_files) -> str | None:
    """Convert uploaded files into a single SQLite database. Returns the db path."""
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


# ── Sidebar (always visible) ────────────────────────────────────────────────
with st.sidebar:
    if st.session_state.page != "upload":
        st.divider()
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
                st.success(f"✅ {st.session_state.db_name} — {len(schema)} tables")
                st.header("🗂 Tables")
                for table, cols in schema.items():
                    with st.expander(f"**{table}** ({len(cols)} cols)"):
                        for c in cols:
                            st.markdown(f"- `{c['name']}` *{c['type']}*")

    if st.session_state.page == "query" and st.session_state.history:
        st.divider()
        st.header("🕒 Query History")
        for i, entry in enumerate(reversed(st.session_state.history[-10:])):
            with st.expander(f"Q{len(st.session_state.history) - i}: {entry['question'][:50]}…"):
                st.code(entry["sql"], language="sql")
                if entry.get("insight"):
                    st.caption(entry["insight"])


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: Upload / Welcome
# ═══════════════════════════════════════════════════════════════════════════
if st.session_state.page == "upload":
    st.title("📊 BI Assistant")
    st.markdown("""
    **Turn any dataset into insights — no SQL required.**

    Upload your data and ask business questions in plain English.
    The AI will write SQL queries, generate charts, and provide actionable insights.

    ### Getting Started
    1. **Upload** a SQLite database (`.sqlite` / `.db`) or one or more CSV files.
    2. **Review** the auto-generated data overview.
    3. **Ask questions** and get instant answers with charts and insights.
    """)

    st.divider()

    uploaded = st.file_uploader(
        "Upload your data",
        type=["sqlite", "db", "csv"],
        accept_multiple_files=True,
        help="Upload a .sqlite/.db file, or multiple .csv files (each becomes a table).",
    )

    demo_path = Path(__file__).parent / "data" / "olist.sqlite"
    col1, col2 = st.columns(2)

    with col1:
        upload_btn = st.button(
            "🚀 Upload & Analyze",
            type="primary",
            disabled=not uploaded,
            use_container_width=True,
        )

    with col2:
        demo_available = demo_path.exists()
        demo_btn = st.button(
            "📦 Use Demo Dataset (Olist)",
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
            st.rerun()
        else:
            st.error("Unsupported file type. Please upload .sqlite, .db, or .csv files.")

    if demo_btn:
        st.session_state.db_path = str(demo_path)
        st.session_state.db_name = "Olist E-Commerce (demo)"
        st.session_state.ai_overview = None
        st.session_state.table_stats = None
        st.session_state.page = "overview"
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: Data Overview
# ═══════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "overview":
    st.title("📊 Data Overview")
    st.caption(f"Dataset: **{st.session_state.db_name}**")

    conn = get_db_connection()
    if conn is None:
        st.error("Could not connect to the database.")
        st.stop()

    schema = get_schema(conn)
    if not schema:
        st.error("No tables found in the database.")
        st.stop()

    # Compute table stats once
    if st.session_state.table_stats is None:
        st.session_state.table_stats = get_table_stats(conn, schema)
    table_stats = st.session_state.table_stats

    # Summary metrics
    total_rows = sum(info["rows"] for info in table_stats.values())
    total_tables = len(schema)
    total_cols = sum(info["cols"] for info in table_stats.values())

    m1, m2, m3 = st.columns(3)
    m1.metric("Tables", total_tables)
    m2.metric("Total Columns", total_cols)
    m3.metric("Total Rows", f"{total_rows:,}")

    st.divider()

    # Table details
    st.subheader("🗂 Tables")
    for table, cols in schema.items():
        info = table_stats[table]
        with st.expander(f"**{table}** — {info['rows']:,} rows, {info['cols']} columns"):
            col_names = [c["name"] for c in cols]
            col_types = [c["type"] for c in cols]
            st.markdown("**Columns:** " + ", ".join(f"`{n}` ({t})" for n, t in zip(col_names, col_types)))
            st.caption("Preview (first 5 rows):")
            st.dataframe(info["preview"], use_container_width=True, hide_index=True)

    st.divider()

    # AI Overview
    if st.session_state.ai_overview:
        st.subheader("🧠 AI Overview")
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
                st.rerun()

    st.divider()

    if st.button("🔍 Start Querying", type="primary", use_container_width=True):
        st.session_state.page = "query"
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: Query Interface
# ═══════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "query":
    st.title("📊 BI Assistant")
    st.caption(f"Dataset: **{st.session_state.db_name}** — Ask business questions in plain English.")

    conn = get_db_connection()
    if conn is None:
        st.error("Database not connected. Please upload data first.")
        if st.button("Go to Upload"):
            st.session_state.page = "upload"
            st.rerun()
        st.stop()

    schema = get_schema(conn)

    # Example questions
    with st.sidebar:
        st.divider()
        st.header("💡 Example Questions")
        examples = [
            "What are the top 5 categories by total revenue?",
            "How many records are in each table?",
            "What is the distribution of values in the largest table?",
            "Show me monthly trends over time.",
        ]
        for ex in examples:
            if st.button(ex, key=f"ex_{ex[:30]}", use_container_width=True):
                st.session_state["prefill_question"] = ex

    prefill = st.session_state.pop("prefill_question", "")
    question = st.text_area(
        "Ask a business question:",
        value=prefill,
        placeholder="e.g. What are the top 5 categories by revenue?",
        height=90,
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

            st.write("Generating insight…")
            insight = generate_insight(client, question, df)
            status.update(label="✅ Done", state="complete", expanded=False)

        st.subheader("🔎 Generated SQL")
        st.code(sql, language="sql")

        if df.empty:
            st.info("Query returned no rows.")
        else:
            col_chart, col_table = st.columns([3, 2])

            with col_chart:
                st.subheader("📈 Chart")
                if len(df) > MAX_ROWS_FOR_CHART:
                    st.caption(f"Showing chart for first {MAX_ROWS_FOR_CHART} rows.")
                    auto_chart(df.head(MAX_ROWS_FOR_CHART), question)
                else:
                    auto_chart(df, question)

            with col_table:
                st.subheader("📋 Data")
                st.dataframe(df, use_container_width=True, height=380)
                csv = df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    "⬇️ Export CSV",
                    data=csv,
                    file_name="query_results.csv",
                    mime="text/csv",
                )

        st.subheader("🧠 AI Insight")
        st.info(insight)

        st.session_state.history.append(
            {"question": question, "sql": sql, "insight": insight}
        )

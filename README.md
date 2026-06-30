# 📊 BI Assistant

A natural-language BI tool that lets you upload any dataset and ask business questions in plain English. Powered by Google Gemini and Streamlit.

**[Try it live →](https://bi-assistant-9gsky5cwnxdg2qnswa6fmr.streamlit.app/)**

## Features

- **Upload any data** — SQLite databases (`.sqlite`, `.db`) or CSV files (each CSV becomes a table)
- **AI-powered data overview** — automatic schema detection with an optional AI-generated summary of your dataset
- **Natural language queries** — ask questions in English and get SQL, interactive charts, and actionable insights
- **Auto-charting** — automatically picks the right chart type (bar, line, scatter) based on your data and question
- **Export** — download query results as CSV

## Usage

1. **Upload** your data (SQLite database or CSV files) on the welcome screen
2. **Review** the auto-detected schema and table stats — optionally generate an AI overview
3. **Start querying** — type a question like *"What are the top 5 categories by revenue?"* and get SQL + charts + insights

### Demo Dataset

Place the [Olist e-commerce dataset](https://www.kaggle.com/datasets/terencicp/e-commerce-dataset-by-olist-as-an-sqlite-database) as `data/olist.sqlite` to use the built-in demo option.

## Running Locally

```bash
git clone https://github.com/andrewnguyxn/BI-Assistant.git
cd BI-Assistant
pip install -r requirements.txt
GOOGLE_API_KEY=your_key streamlit run app.py
```

Get a free API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Tech Stack

- **Frontend**: Streamlit
- **AI**: Google Gemini (via `google-genai`)
- **Database**: SQLite
- **Charts**: Plotly Express

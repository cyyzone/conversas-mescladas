import os
from io import BytesIO
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from openpyxl.styles import Font
from requests import RequestException
from streamlit.errors import StreamlitSecretNotFoundError

from relatorio_mesclagens import build_report

load_dotenv()

REPORT_TIMEZONE = ZoneInfo("America/Sao_Paulo")
CSV_PATH = Path(__file__).with_name("conversas_mescladas.csv")
REASON_FIELDS = {
    "secundaria": ("motivo_contato_secundaria", "motivo_contato"),
    "principal": ("motivo_contato_principal",),
}
MISSING_REASON = "Sem motivo"
DUPLICATE_REASON = "Conversa duplicada"


def local_day_bounds(start_date, end_date):
    start = datetime.combine(start_date, time.min, REPORT_TIMEZONE)
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1), time.min, REPORT_TIMEZONE
    )
    return int(start.timestamp()), int(end_exclusive.timestamp()) - 1


def configured_value(name):
    try:
        value = st.secrets.get(name, os.environ.get(name, ""))
    except StreamlitSecretNotFoundError:
        value = os.environ.get(name, "")
    return str(value).strip()


def reason_for(row, side):
    for field in REASON_FIELDS[side]:
        value = row.get(field)
        if value is None or pd.isna(value):
            continue

        reason = str(value).strip()
        if reason and reason.casefold() not in {"nan", "none", "null"}:
            return reason

    return MISSING_REASON


def is_duplicate(reason):
    return "duplic" in reason.casefold()


def filter_csv_rows(start_date, end_date):
    if not CSV_PATH.exists():
        return []

    data = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    if "mesclada_em_utc" not in data.columns:
        return data.to_dict("records")

    timestamps = pd.to_datetime(data["mesclada_em_utc"], utc=True, errors="coerce")
    local_dates = timestamps.dt.tz_convert(REPORT_TIMEZONE).dt.date
    selected = (local_dates >= start_date) & (local_dates <= end_date)
    return data.loc[selected].to_dict("records")


def make_reason_counts(rows, side):
    counts = {}
    for row in rows:
        reason = reason_for(row, side)
        counts[reason] = counts.get(reason, 0) + 1

    total = sum(counts.values())
    result = pd.DataFrame(
        [
            {
                "motivo": reason,
                "quantidade": count,
                "percentual": count * 100 / total if total else 0,
            }
            for reason, count in counts.items()
        ],
        columns=["motivo", "quantidade", "percentual"],
    )
    return result.sort_values("quantidade", ascending=False, ignore_index=True)


def make_classification_counts(rows, side):
    counts = {
        DUPLICATE_REASON: 0,
        "Outros motivos": 0,
        MISSING_REASON: 0,
    }
    for row in rows:
        reason = reason_for(row, side)
        if reason == MISSING_REASON:
            counts[MISSING_REASON] += 1
        elif is_duplicate(reason):
            counts[DUPLICATE_REASON] += 1
        else:
            counts["Outros motivos"] += 1

    total = sum(counts.values())
    return pd.DataFrame(
        [
            {
                "categoria": category,
                "quantidade": count,
                "percentual": count * 100 / total if total else 0,
            }
            for category, count in counts.items()
            if count
        ]
    )


def make_comparison_data(rows):
    records = []
    for side, label in (
        ("secundaria", "Conversa secundária"),
        ("principal", "Conversa principal"),
    ):
        counts = make_reason_counts(rows, side)
        total = int(counts["quantidade"].sum())
        for item in counts.to_dict("records"):
            records.append(
                {
                    "motivo": item["motivo"],
                    "conversa": label,
                    "quantidade": item["quantidade"],
                    "percentual": (
                        item["quantidade"] * 100 / total if total else 0
                    ),
                }
            )

    comparison = pd.DataFrame(records)
    if comparison.empty:
        return comparison

    top_reasons = (
        comparison.groupby("motivo")["quantidade"]
        .sum()
        .nlargest(12)
        .index
        .tolist()
    )
    return comparison[comparison["motivo"].isin(top_reasons)]


def conversation_url(app_id, admin_id, conversation_id):
    if not app_id or not admin_id or not conversation_id:
        return ""
    return (
        "https://app.intercom.com/a/inbox/"
        f"{quote(app_id, safe='')}/inbox/admin/{quote(admin_id, safe='')}/conversation/"
        f"{quote(str(conversation_id), safe='')}"
    )


def make_conversation_table(rows, app_id, admin_id):
    columns = {
        "mesclada_em_utc": "Mesclada em (UTC)",
        "id_secundaria_mesclada": "ID da conversa secundária",
        "motivo_contato_secundaria": "Motivo da secundária",
        "status_secundaria": "Status da secundária",
        "id_principal": "ID da conversa principal",
        "motivo_contato_principal": "Motivo da principal",
        "status_principal": "Status da principal",
    }
    table = pd.DataFrame(
        [
            {
                column: (
                    conversation_url(
                        app_id, admin_id, row.get("id_secundaria_mesclada")
                    )
                    if column == "id_secundaria_mesclada"
                    else conversation_url(
                        app_id, admin_id, row.get("id_principal")
                    )
                    if column == "id_principal"
                    else row.get(column, "")
                )
                for column in columns
            }
            for row in rows
        ]
    )
    return table.rename(columns=columns)


def make_excel_file(table):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        table.to_excel(writer, index=False, sheet_name="Conversas")
        worksheet = writer.sheets["Conversas"]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
        for column_name in (
            "ID da conversa secundária",
            "ID da conversa principal",
        ):
            column_index = table.columns.get_loc(column_name) + 1
            for row_index in range(2, len(table) + 2):
                cell = worksheet.cell(row=row_index, column=column_index)
                if cell.value:
                    url = str(cell.value)
                    cell.value = url.rsplit("/", 1)[-1]
                    cell.hyperlink = url
                    cell.style = "Hyperlink"
    return output.getvalue()


def donut_chart(data, label_field, title):
    chart = (
        alt.Chart(data)
        .mark_arc(innerRadius=75, stroke="white", strokeWidth=2)
        .encode(
            theta=alt.Theta("quantidade:Q", title="Quantidade"),
            color=alt.Color(
                f"{label_field}:N",
                title=title,
                scale=alt.Scale(scheme="tableau20"),
            ),
            tooltip=[
                alt.Tooltip(f"{label_field}:N", title=title),
                alt.Tooltip("quantidade:Q", title="Quantidade"),
                alt.Tooltip("percentual:Q", title="Percentual", format=".1f"),
            ],
        )
        .properties(height=340)
    )
    return chart


st.set_page_config(page_title="Relatório de Mesclagens", layout="wide")
st.title("Dashboard de mesclagens do Intercom")

with st.sidebar:
    st.header("Filtros")
    token = configured_value("INTERCOM_TOKEN")
    intercom_app_id = configured_value("INTERCOM_APP_ID")
    intercom_admin_id = configured_value("INTERCOM_ADMIN_ID")
    default_end = date.today()
    default_start = default_end - timedelta(days=30)
    selected_dates = st.date_input(
        "Período da mesclagem",
        value=(default_start, default_end),
        min_value=date(2024, 1, 1),
        max_value=default_end,
    )
    update_report = st.button("Atualizar relatório", type="primary")

if len(selected_dates) != 2:
    st.info("Selecione as datas inicial e final para consultar o relatório.")
    st.stop()

start_date, end_date = selected_dates
if start_date > end_date:
    st.error("A data inicial não pode ser posterior à data final.")
    st.stop()

progress_value = st.session_state.get("report_progress", 0.0)
with st.expander("Andamento da consulta", expanded=update_report):
    progress_bar = st.progress(
        progress_value,
        text=st.session_state.get(
            "report_progress_text",
            "Aguardando clique em Atualizar relatório.",
        ),
    )
    progress_status = st.empty()
    progress_status.info(
        st.session_state.get(
            "report_progress_status",
            "Aguardando clique em Atualizar relatório.",
        )
    )


def on_report_progress(event):
    message = event["message"]
    timestamp = datetime.now(REPORT_TIMEZONE).strftime("%H:%M:%S")
    status_message = f"{timestamp} — {message}"
    st.session_state["report_progress_status"] = status_message

    progress = event.get("progress")
    if progress is not None:
        progress = min(max(float(progress), 0.0), 1.0)
        st.session_state["report_progress"] = progress
        st.session_state["report_progress_text"] = (
            f"{progress:.0%} — {message}"
        )
        progress_bar.progress(
            progress,
            text=st.session_state["report_progress_text"],
        )
    else:
        progress_bar.progress(
            st.session_state.get("report_progress", 0.0),
            text=status_message,
        )
    progress_status.info(status_message)


if update_report:
    if not token and not CSV_PATH.exists():
        st.error(
            "Credenciais não configuradas. Adicione INTERCOM_TOKEN, INTERCOM_APP_ID "
            "e INTERCOM_ADMIN_ID em Settings > Secrets no Streamlit."
        )
        st.stop()

    st.session_state["report_progress"] = 0.0
    st.session_state["report_progress_text"] = "Iniciando a consulta..."
    start_message = (
        f"{datetime.now(REPORT_TIMEZONE):%H:%M:%S} — Iniciando a consulta."
    )
    st.session_state["report_progress_status"] = start_message
    progress_bar.progress(0.0, text="Iniciando a consulta...")
    progress_status.info(start_message)

    try:
        if token:
            since_timestamp, until_timestamp = local_day_bounds(start_date, end_date)
            with st.spinner("Consultando as mesclagens no Intercom..."):
                st.session_state["report_rows"] = build_report(
                    token,
                    since_timestamp,
                    until_timestamp,
                    progress_callback=on_report_progress,
                )
            st.session_state["report_source"] = "API do Intercom"
        else:
            on_report_progress(
                {
                    "message": "Lendo as mesclagens do CSV local.",
                    "progress": 0.2,
                }
            )
            st.session_state["report_rows"] = filter_csv_rows(start_date, end_date)
            st.session_state["report_source"] = str(CSV_PATH.name)
            on_report_progress(
                {
                    "message": (
                        f"Leitura concluída: {len(st.session_state['report_rows'])} "
                        "mesclagens no CSV."
                    ),
                    "progress": 1.0,
                }
            )

        st.session_state["report_period"] = (start_date, end_date)
        st.session_state["report_progress"] = 1.0
        st.session_state["report_progress_text"] = (
            f"100% — Consulta concluída: "
            f"{len(st.session_state['report_rows'])} pares localizados."
        )
        complete_message = (
            f"{datetime.now(REPORT_TIMEZONE):%H:%M:%S} — "
            f"{len(st.session_state['report_rows'])} pares localizados."
        )
        st.session_state["report_progress_status"] = complete_message
        progress_bar.progress(
            1.0,
            text=st.session_state["report_progress_text"],
        )
        progress_status.success(complete_message)
    except RequestException as error:
        error_message = f"Falha ao consultar a API do Intercom: {error}"
        st.session_state["report_progress_text"] = error_message
        status_message = (
            f"{datetime.now(REPORT_TIMEZONE):%H:%M:%S} — {error_message}"
        )
        st.session_state["report_progress_status"] = status_message
        progress_status.error(status_message)
        st.stop()

if "report_rows" not in st.session_state:
    st.info("Selecione o período e clique em **Atualizar relatório** para pesquisar.")
    st.stop()

rows = st.session_state["report_rows"]
loaded_period = st.session_state.get("report_period")
if loaded_period != (start_date, end_date):
    st.info(
        "O período selecionado mudou. Clique em **Atualizar relatório** para pesquisar "
        "esse período. Abaixo continuam exibidos os últimos resultados carregados."
    )

if not rows:
    st.info("Nenhuma mesclagem encontrada no último relatório carregado.")
    st.caption(f"Fonte dos dados: {st.session_state.get('report_source', 'CSV local')}.")
    st.stop()

secondary_counts = make_reason_counts(rows, "secundaria")
primary_counts = make_reason_counts(rows, "principal")
secondary_classification = make_classification_counts(rows, "secundaria")
primary_classification = make_classification_counts(rows, "principal")
comparison_data = make_comparison_data(rows)

duplicate_count = sum(
    1
    for row in rows
    if is_duplicate(reason_for(row, "secundaria"))
)
total = len(rows)
col1, col2, col3 = st.columns(3)
col1.metric("Total de mesclagens", total)
col2.metric(
    "Duplicadas (secundárias)",
    f"{duplicate_count} ({duplicate_count * 100 / total:.1f}%)",
)
col3.metric(
    "Período carregado",
    f"{loaded_period[0].strftime('%d/%m/%Y')} a {loaded_period[1].strftime('%d/%m/%Y')}",
)

conversation_table = make_conversation_table(
    rows, intercom_app_id, intercom_admin_id
)
st.subheader("Conversas localizadas")
st.caption(
    "A secundária é a conversa mesclada; a principal é a conversa que permaneceu."
)
if not intercom_app_id or not intercom_admin_id:
    st.info(
        "Configure INTERCOM_APP_ID e INTERCOM_ADMIN_ID nos Secrets do Streamlit "
        "para habilitar os links."
    )
st.dataframe(
    conversation_table,
    width="stretch",
    hide_index=True,
    column_config={
        "ID da conversa secundária": st.column_config.LinkColumn(
            "ID da conversa secundária",
            display_text=r".*/conversation/([^/?]+)$",
        ),
        "ID da conversa principal": st.column_config.LinkColumn(
            "ID da conversa principal",
            display_text=r".*/conversation/([^/?]+)$",
        ),
    },
)
st.download_button(
    "Exportar conversas para Excel",
    data=make_excel_file(conversation_table),
    file_name=f"conversas_mescladas_{start_date:%Y%m%d}_{end_date:%Y%m%d}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

overview_tab, attributes_tab, comparison_tab = st.tabs(
    ["Resumo", "Motivos por atributo", "Comparação"]
)

with overview_tab:
    st.subheader("Conversa duplicada x outros motivos")
    secondary_chart_data = secondary_classification.rename(
        columns={"categoria": "classificacao"}
    )
    primary_chart_data = primary_classification.rename(
        columns={"categoria": "classificacao"}
    )
    secondary_col, primary_col = st.columns(2)
    with secondary_col:
        st.markdown("**Atributo da conversa secundária**")
        st.altair_chart(
            donut_chart(
                secondary_chart_data,
                "classificacao",
                "Classificação",
            ),
            width="stretch",
        )
    with primary_col:
        st.markdown("**Atributo da conversa principal**")
        st.altair_chart(
            donut_chart(
                primary_chart_data,
                "classificacao",
                "Classificação",
            ),
            width="stretch",
        )

with attributes_tab:
    st.subheader("Distribuição por motivo de contato nos atributos")
    secondary_col, primary_col = st.columns(2)
    with secondary_col:
        st.markdown("**Motivo da conversa secundária**")
        st.altair_chart(
            donut_chart(secondary_counts, "motivo", "Motivo de contato"),
            width="stretch",
        )
        st.dataframe(
            secondary_counts.rename(
                columns={
                    "motivo": "Motivo",
                    "quantidade": "Quantidade",
                    "percentual": "Percentual",
                }
            ).style.format({"Percentual": "{:.1f}%"}),
            width="stretch",
            hide_index=True,
        )
    with primary_col:
        st.markdown("**Motivo da conversa principal**")
        st.altair_chart(
            donut_chart(primary_counts, "motivo", "Motivo de contato"),
            width="stretch",
        )
        st.dataframe(
            primary_counts.rename(
                columns={
                    "motivo": "Motivo",
                    "quantidade": "Quantidade",
                    "percentual": "Percentual",
                }
            ).style.format({"Percentual": "{:.1f}%"}),
            width="stretch",
            hide_index=True,
        )

with comparison_tab:
    st.subheader("Comparação dos motivos nas conversas secundária e principal")
    if comparison_data.empty:
        st.info("Não há motivos disponíveis para comparar.")
    else:
        comparison_chart = (
            alt.Chart(comparison_data)
            .mark_bar()
            .encode(
                x=alt.X("percentual:Q", title="Percentual dentro do atributo (%)"),
                y=alt.Y(
                    "motivo:N",
                    title="Motivo de contato",
                    sort="-x",
                ),
                color=alt.Color("conversa:N", title="Atributo"),
                yOffset="conversa:N",
                tooltip=[
                    alt.Tooltip("motivo:N", title="Motivo"),
                    alt.Tooltip("conversa:N", title="Atributo"),
                    alt.Tooltip("quantidade:Q", title="Quantidade"),
                    alt.Tooltip("percentual:Q", title="Percentual", format=".1f"),
                ],
            )
            .properties(height=420)
        )
        st.altair_chart(comparison_chart, width="stretch")
        st.caption(
            "A comparação usa percentual dentro de cada atributo, além da quantidade no detalhe ao passar o cursor."
        )

st.caption(
    f"Fonte: {st.session_state.get('report_source', 'dados carregados')}."
)

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import local
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.intercom.io"
API_VERSION = "2.16"
PAGE_SIZE = 150
HTTP_SESSION = requests.Session()
THREAD_LOCAL = local()
DETAIL_WORKERS = 5
MERGE_PRIMARY_PART = "merged_primary_conversation"
MERGE_SECONDARY_PART = "merged_secondary_conversation"
MERGE_EVENT_TOLERANCE_SECONDS = 5
PRIMARY_UPDATE_GRACE_SECONDS = 7 * 24 * 60 * 60
REPORT_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def api_get(token, path, params=None):
    session = getattr(THREAD_LOCAL, "session", HTTP_SESSION)
    response = session.get(
        f"{API_URL}{path}",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Intercom-Version": API_VERSION,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def api_search(token, payload):
    response = HTTP_SESSION.post(
        f"{API_URL}/conversations/search",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Intercom-Version": API_VERSION,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def list_conversations(
    token, since_timestamp=None, until_timestamp=None, progress_callback=None
):
    conversations = []
    starting_after = None
    page_number = 0
    total_count = None
    while True:
        if progress_callback:
            progress_callback(
                {
                    "stage": "list",
                    "message": f"Consultando página {page_number + 1} da API...",
                    "progress": None,
                }
            )
        filters = []
        if since_timestamp is not None:
            filters.append({
                "field": "updated_at",
                "operator": ">",
                "value": str(since_timestamp),
            })
        if until_timestamp is not None:
            filters.append({
                "field": "updated_at",
                "operator": "<",
                "value": str(until_timestamp),
            })

        if filters:
            payload = {
                "query": filters[0] if len(filters) == 1 else {
                    "operator": "AND",
                    "value": filters,
                },
                "pagination": {"per_page": PAGE_SIZE},
            }
            if starting_after:
                payload["pagination"]["starting_after"] = starting_after
            result = api_search(token, payload)
        else:
            params = {"per_page": PAGE_SIZE}
            if starting_after:
                params["starting_after"] = starting_after
            result = api_get(token, "/conversations", params=params)

        conversations.extend(result.get("conversations", []))
        page_number += 1
        response_total = result.get("total_count")
        if response_total is not None:
            try:
                total_count = int(response_total)
            except (TypeError, ValueError):
                total_count = None
        if progress_callback:
            progress = (
                min(len(conversations) / total_count, 1.0) * 0.15
                if total_count
                else None
            )
            total_message = f" de {total_count}" if total_count is not None else ""
            count_note = (
                ""
                if total_count is not None
                else " (total ainda não informado pela API)"
            )
            progress_callback(
                {
                    "stage": "list",
                    "message": (
                        f"Página {page_number}: recebidas {len(conversations)}"
                        f"{total_message} conversas{count_note}."
                    ),
                    "progress": progress,
                }
            )
        next_page = (result.get("pages") or {}).get("next") or {}
        starting_after = next_page.get("starting_after")
        if not starting_after:
            if progress_callback:
                progress_callback(
                    {
                        "stage": "listed",
                        "message": (
                            f"Busca inicial concluída: {len(conversations)} "
                            "conversas recebidas da API."
                        ),
                        "progress": 0.15,
                    }
                )
            return conversations


def conversation_parts(conversation):
    return (
        (conversation.get("conversation_parts") or {}).get("conversation_parts") or []
    )


def merge_event_times(conversation, event_type):
    return [
        part["created_at"]
        for part in conversation_parts(conversation)
        if part.get("part_type") == event_type and part.get("created_at") is not None
    ]


def timestamp_to_iso(timestamp):
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def conversation_title(conversation):
    title = conversation.get("title")
    if not title:
        title = (conversation.get("source") or {}).get("subject")
    return str(title).strip() if title else ""


def conversation_contact_ids(conversation):
    contacts = (conversation.get("contacts") or {}).get("contacts") or []
    return {
        str(contact["id"])
        for contact in contacts
        if contact.get("id") is not None
    }


def is_merged_secondary(summary):
    value = (summary.get("custom_attributes") or {}).get("Merged")
    return value is True or (isinstance(value, str) and value.casefold() == "true")


def build_report(
    token, since_timestamp=None, until_timestamp=None, progress_callback=None
):
    def report_progress(stage, message, progress=None):
        if progress_callback:
            progress_callback(
                {"stage": stage, "message": message, "progress": progress}
            )

    search_since_timestamp = (
        max(0, since_timestamp - PRIMARY_UPDATE_GRACE_SECONDS)
        if since_timestamp is not None
        else None
    )
    search_until_timestamp = (
        until_timestamp + PRIMARY_UPDATE_GRACE_SECONDS
        if until_timestamp is not None
        else None
    )
    report_progress("list", "Iniciando a busca de conversas na API.")
    summaries = list_conversations(
        token,
        search_since_timestamp,
        search_until_timestamp,
        progress_callback=progress_callback,
    )
    summaries_by_id = {
        str(summary["id"]): summary
        for summary in summaries
        if summary.get("id") is not None
    }
    details = {}
    details_by_contact = {}
    indexed_details = set()

    def index_details(conversation_id, detail):
        if conversation_id in indexed_details:
            return
        indexed_details.add(conversation_id)
        for contact_id in conversation_contact_ids(detail):
            details_by_contact.setdefault(contact_id, set()).add(conversation_id)

    def fetch_details(
        conversation_ids,
        index_contacts=True,
        progress_start=None,
        progress_end=None,
        stage="details",
    ):
        missing_ids = [
            conversation_id
            for conversation_id in conversation_ids
            if conversation_id not in details
        ]
        if not missing_ids:
            return

        stage_label = "secundárias" if stage == "secondary_details" else "candidatas"
        report_progress(
            stage,
            f"Solicitando detalhes de {len(missing_ids)} conversas {stage_label}...",
        )

        def fetch_detail(conversation_id):
            if not hasattr(THREAD_LOCAL, "session"):
                THREAD_LOCAL.session = requests.Session()
            return conversation_id, api_get(
                token, f"/conversations/{conversation_id}"
            )

        with ThreadPoolExecutor(
            max_workers=min(DETAIL_WORKERS, len(missing_ids))
        ) as executor:
            futures = [
                executor.submit(fetch_detail, conversation_id)
                for conversation_id in missing_ids
            ]
            for completed, future in enumerate(as_completed(futures), start=1):
                conversation_id, detail = future.result()
                details[conversation_id] = detail
                if index_contacts:
                    index_details(conversation_id, detail)
                progress = None
                if progress_start is not None and progress_end is not None:
                    progress = progress_start + (
                        (progress_end - progress_start) * completed / len(futures)
                    )
                report_progress(
                    stage,
                    f"Detalhes de {stage_label} carregados: "
                    f"{completed} de {len(futures)}.",
                    progress,
                )

    def event_is_in_range(event_time):
        return (
            (since_timestamp is None or event_time >= since_timestamp)
            and (until_timestamp is None or event_time <= until_timestamp)
        )

    rows = {}
    secondary_summaries = [
        summary
        for summary in summaries
        if summary.get("state") == "closed" and is_merged_secondary(summary)
    ]
    pending_secondary_ids = {
        str(summary["id"])
        for summary in secondary_summaries
        if summary.get("id") is not None and str(summary["id"])
    }
    report_progress(
        "secondaries",
        f"{len(secondary_summaries)} conversas secundárias identificadas; "
        "carregando detalhes.",
        0.15,
    )
    fetch_details(
        pending_secondary_ids,
        index_contacts=False,
        progress_start=0.15,
        progress_end=0.30,
        stage="secondary_details",
    )
    if not secondary_summaries:
        report_progress(
            "complete",
            "Nenhuma conversa secundária mesclada foi localizada.",
            1.0,
        )
        return []

    for secondary_index, secondary_summary in enumerate(
        secondary_summaries, start=1
    ):
        secondary_progress_end = 0.30 + (
            0.65 * secondary_index / len(secondary_summaries)
        )
        secondary_id = str(secondary_summary.get("id", ""))
        pending_secondary_ids.discard(secondary_id)
        secondary = details.get(secondary_id)
        if not secondary:
            report_progress(
                "matching",
                f"Secundária {secondary_index} de {len(secondary_summaries)} "
                "sem detalhes disponíveis; continuando.",
                secondary_progress_end,
            )
            continue
        index_details(secondary_id, secondary)
        contact_ids = conversation_contact_ids(secondary)

        for secondary_event_time in merge_event_times(secondary, MERGE_SECONDARY_PART):
            if not event_is_in_range(secondary_event_time):
                continue

            candidate_ids = {
                str(candidate_id)
                for contact_id in contact_ids
                for candidate_id in details_by_contact.get(contact_id, set())
                if candidate_id != secondary_id
            }
            candidate_ids.update(
                candidate_id
                for contact_id in contact_ids
                for candidate_id, candidate in summaries_by_id.items()
                if candidate_id != secondary_id
                and (
                    candidate_id not in details
                    or candidate_id in pending_secondary_ids
                )
                and contact_id in conversation_contact_ids(candidate)
                and candidate.get("updated_at", 0)
                >= secondary_event_time - MERGE_EVENT_TOLERANCE_SECONDS
                and candidate.get("updated_at", 0)
                <= secondary_event_time + PRIMARY_UPDATE_GRACE_SECONDS
            )
            candidate_summaries = {
                primary_id: summaries_by_id[primary_id]
                for primary_id in candidate_ids
                if primary_id in summaries_by_id
            }
            report_progress(
                "matching",
                f"Secundária {secondary_index} de {len(secondary_summaries)}: "
                f"{len(candidate_summaries)} conversas candidatas para verificar; "
                f"{len(rows)} pares confirmados até agora.",
            )
            fetch_details(candidate_summaries)

            candidate_primary_ids = set()
            for primary_id in candidate_summaries:
                primary = details[primary_id]
                if not primary:
                    continue
                index_details(primary_id, primary)
                if any(
                    event_is_in_range(primary_event_time)
                    and abs(primary_event_time - secondary_event_time)
                    <= MERGE_EVENT_TOLERANCE_SECONDS
                    for primary_event_time in merge_event_times(
                        primary, MERGE_PRIMARY_PART
                    )
                ):
                    candidate_primary_ids.add(primary_id)

            if len(candidate_primary_ids) != 1:
                continue

            primary_id = candidate_primary_ids.pop()
            primary = details[primary_id]
            secondary_reason = (secondary.get("custom_attributes") or {}).get(
                "Motivo de Contato"
            )
            primary_reason = (primary.get("custom_attributes") or {}).get(
                "Motivo de Contato"
            )
            rows[(secondary_id, primary_id)] = {
                "id_secundaria_mesclada": secondary_id,
                "titulo_secundaria": conversation_title(secondary),
                "id_principal": primary_id,
                "titulo_principal": conversation_title(primary),
                "motivo_contato_secundaria": (
                    str(secondary_reason)
                    if secondary_reason not in (None, "")
                    else ""
                ),
                "motivo_contato_principal": (
                    str(primary_reason) if primary_reason not in (None, "") else ""
                ),
                "status_secundaria": secondary.get("state", ""),
                "status_principal": primary.get("state", ""),
                "criada_secundaria_em_utc": timestamp_to_iso(secondary.get("created_at")),
                "mesclada_em_utc": timestamp_to_iso(secondary_event_time),
            }

        report_progress(
            "matching",
            f"Analisadas {secondary_index} de {len(secondary_summaries)} "
            f"secundárias; {len(rows)} pares principal/secundária confirmados.",
            secondary_progress_end,
        )

    report_progress(
        "complete",
        f"Consulta concluída: {len(rows)} pares principal/secundária localizados.",
        1.0,
    )
    return list(rows.values())


def parse_since(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use a data no formato AAAA-MM-DD.") from error
    return int(parsed.timestamp())


def day_bounds(value):
    selected_day = datetime.strptime(value, "%Y-%m-%d").date()
    start = datetime.combine(selected_day, time.min, REPORT_TIMEZONE)
    next_day = datetime.combine(selected_day + timedelta(days=1), time.min, REPORT_TIMEZONE)
    return int(start.timestamp()), int(next_day.timestamp()) - 1


def main():
    parser = argparse.ArgumentParser(
        description="Gera CSV com conversas secundárias mescladas e suas conversas principais."
    )
    parser.add_argument(
        "--since",
        type=parse_since,
        help="Considera conversas atualizadas a partir desta data (AAAA-MM-DD).",
    )
    parser.add_argument(
        "--output",
        default="conversas_mescladas.csv",
        help="Caminho do CSV de saída (padrão: conversas_mescladas.csv).",
    )
    args = parser.parse_args()

    token = os.environ.get("INTERCOM_TOKEN")
    if not token:
        print("Defina a variável de ambiente INTERCOM_TOKEN.", file=sys.stderr)
        return 1

    try:
        rows = build_report(token, args.since)
        fields = [
            "id_secundaria_mesclada",
            "titulo_secundaria",
            "id_principal",
            "titulo_principal",
            "motivo_contato_secundaria",
            "motivo_contato_principal",
            "status_secundaria",
            "status_principal",
            "criada_secundaria_em_utc",
            "mesclada_em_utc",
        ]
        with Path(args.output).open("w", newline="", encoding="utf-8-sig") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    except requests.RequestException as error:
        print(f"Erro ao consultar a API do Intercom: {error}", file=sys.stderr)
        return 1

    print(f"Relatório gerado: {args.output} ({len(rows)} mesclagem(ns)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

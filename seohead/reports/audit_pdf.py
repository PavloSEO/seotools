# ruff: noqa: RUF001 -- Intentional Russian labels in the localized report dictionary.
"""Render a retained technical-audit PDF model as self-contained localized HTML."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from seohead.reports.svg_charts import Series, bar_chart
from seohead.reports.traffic_dashboard import Brand, load_brand

_MODEL_SCHEMA = "seohead.technical-audit-pdf/1"

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "report": "Technical SEO audit",
        "source_site_audit": "Site audit",
        "source_sf_audit": "Screaming Frog audit",
        "audit_for": "Audit for",
        "prepared": "Generated",
        "scope": "Run scope",
        "state": "Run status",
        "complete": "Complete",
        "complete_note": "Completed within the recorded scope; this does not establish exhaustive site coverage.",
        "partial": "Partial",
        "failed": "Failed",
        "unknown": "Unknown",
        "summary": "Audit summary",
        "summary_field": "Summary field",
        "findings": "Findings",
        "pages": "Affected pages",
        "checks": "Coverage records",
        "check_inventory": "Check inventory",
        "backlog": "Project checklist items",
        "coverage": "Evidence and check coverage",
        "coverage_reported": "Coverage metadata recorded; individual checks may still be unavailable.",
        "findings_by_severity": "Findings by severity",
        "checks_by_state": "Checks by state",
        "capabilities_by_state": "Capability checks by state",
        "critical": "Critical",
        "warning": "Warning",
        "notice": "Notice",
        "ran": "Ran",
        "failed_checks": "Failed",
        "skipped": "Skipped",
        "disabled": "Disabled",
        "finding": "Finding",
        "severity": "Severity",
        "page_url": "Affected URL",
        "status_code": "Status",
        "title": "Page title",
        "observation": "Observation",
        "reproduction": "Recorded evidence",
        "reason": "Reason",
        "check": "Check",
        "state_col": "State",
        "item": "Checklist item",
        "reference": "Source reference",
        "count": "Count",
        "verification": "Verification",
        "omissions": "Projection notes",
        "none": "No items were recorded.",
        "not_reported": "Not reported",
        "unreported": "Not reported",
        "source_reported": "Recorded",
        "not_requested": "Not requested",
        "recorded": "Recorded",
        "unavailable": "Unavailable",
        "declared": "Declared",
        "no_severity_data": "Severity counts were not supplied by the source audit.",
        "no_check_counts": "Check-state counts were not supplied by the source audit.",
        "partial_warning": "This audit is partial. Its counts describe only the recorded scope.",
        "failed_warning": "This audit failed. Findings and coverage may be incomplete.",
        "unknown_warning": "The source does not establish whether this audit completed.",
        "no_reported_scope": "The source did not record a crawl scope.",
        "source": "Source evidence",
        "source_check_coverage": "Source check coverage",
        "group_ran": "Checks run",
        "group_fired": "Checks with findings",
        "group_silent": "Checks run without findings",
        "group_failed": "Failed checks",
        "group_page_tools_failed": "Page checks failed",
        "group_skipped": "Skipped checks",
        "group_disabled": "Disabled checks",
        "group_capabilities": "Check capabilities",
        "group_unreported": "Unclassified checks",
        "group_source_count": "Source records",
        "group_projected_count": "Model records",
        "coverage_details": "Coverage details",
        "unreported_groups": "Counts not reported for",
        "input_diagnostics": "Input diagnostics",
        "crawl_invalid_reason_label": "Invalid crawl reason",
        "crawl_finish_reason_label": "Crawl finish reason",
        "crawl_stopped_reason_label": "Crawl stop reason",
        "report_footer": "Technical audit report",
        "domain": "Site",
        "status_code_label": "HTTP status",
        "occurrences_count_label": "Occurrences count",
        "occurrence_count_label": "Occurrence count",
        "fix_hint_label": "Suggested action",
        "remediation_status_label": "Remediation status",
        "verification_status_label": "Verification status",
        "details_label": "Evidence details",
        "locations_label": "Affected locations",
        "evidence_status": "Status",
        "evidence_reason": "Reason",
        "evidence_id": "Evidence ID",
        "evidence_source_table": "Source table",
        "evidence_observation_id": "Observation ID",
        "evidence_role": "Role",
        "pages_checked_label": "Pages checked",
        "findings_total_label": "Total findings",
        "findings_by_severity_label": "Findings by severity",
        "urls_crawled_label": "URLs crawled",
        "scope_reason_label": "Scope reason",
        "operation_label": "Operation",
        "operation_bounded_site_audit": "Bounded site audit",
        "tools_run": "Tools run",
        "tools_failed": "Tools failed",
        "page_tools_failed": "Page tools failed",
        "evidence_contract": "Evidence contract",
        "capability_rows": "Capability rows",
        "scan_identity_state": "Scan identity status",
        "scan_uuid": "Scan ID",
        "checks_total": "Checks total",
        "failed_pages": "Failed pages",
        "pages_checked": "Pages checked",
        "tool": "Tool",
        "error": "Error",
        "population": "Population",
        "by_severity": "By severity",
        "health_score": "Health score",
        "health_score_scope": "Health score scope",
        "severity_note": "Severity note",
        "title_element_is_missing": "Title element is missing",
        "audit_finding": "Audit finding",
        "capability_records_below": "Capability records listed below",
        "index_label": "Index",
    },
    "ru": {
        "report": "Технический SEO-аудит",
        "source_site_audit": "Аудит сайта",
        "source_sf_audit": "Аудит Screaming Frog",
        "audit_for": "Аудит сайта",
        "prepared": "Сформирован",
        "scope": "Объём проверки",
        "state": "Статус запуска",
        "complete": "Завершён",
        "complete_note": "Завершён в пределах записанного объёма; это не подтверждает полный обход сайта.",
        "partial": "Частичный",
        "failed": "Ошибка",
        "unknown": "Неизвестен",
        "summary": "Итоги аудита",
        "summary_field": "Поле сводки",
        "findings": "Проблемы",
        "pages": "Затронутые страницы",
        "checks": "Записей о покрытии",
        "check_inventory": "Проверок в реестре",
        "backlog": "Задачи проекта",
        "coverage": "Источники данных и покрытие проверок",
        "coverage_reported": "Данные о покрытии записаны; отдельные проверки могут быть недоступны.",
        "findings_by_severity": "Проблемы по важности",
        "checks_by_state": "Проверки по статусу",
        "capabilities_by_state": "Статусы возможностей проверок",
        "critical": "Критические",
        "warning": "Предупреждения",
        "notice": "Замечания",
        "ran": "Выполнены",
        "failed_checks": "С ошибкой",
        "skipped": "Пропущены",
        "disabled": "Отключены",
        "finding": "Проблема",
        "severity": "Важность",
        "page_url": "Затронутый URL",
        "status_code": "Статус",
        "title": "Заголовок страницы",
        "observation": "Наблюдение",
        "reproduction": "Сохранённое свидетельство",
        "reason": "Причина",
        "check": "Проверка",
        "state_col": "Статус",
        "item": "Пункт списка задач",
        "reference": "Источник",
        "count": "Количество",
        "verification": "Проверка исправления",
        "omissions": "Примечания к проекции",
        "none": "Записей нет.",
        "not_reported": "Не указано",
        "unreported": "Не указано",
        "source_reported": "Записаны",
        "not_requested": "Не запрашивалось",
        "recorded": "Записаны",
        "unavailable": "Недоступно",
        "declared": "Заявлено",
        "no_severity_data": "Исходный аудит не содержит счётчиков проблем по важности.",
        "no_check_counts": "Исходный аудит не содержит счётчиков статусов проверок.",
        "partial_warning": "Аудит выполнен частично. Счётчики относятся только к записанному объёму.",
        "failed_warning": "Аудит завершился ошибкой. Данные о проблемах и покрытии могут быть неполными.",
        "unknown_warning": "Источник не подтверждает, завершился ли аудит.",
        "no_reported_scope": "Источник не записал объём обхода.",
        "source": "Сохранённые данные",
        "source_check_coverage": "Покрытие проверок по источнику",
        "group_ran": "Проверки выполнены",
        "group_fired": "Проверки с проблемами",
        "group_silent": "Проверки без проблем",
        "group_failed": "Проверки завершились ошибкой",
        "group_page_tools_failed": "Ошибки проверок страниц",
        "group_skipped": "Пропущенные проверки",
        "group_disabled": "Отключённые проверки",
        "group_capabilities": "Возможности проверок",
        "group_unreported": "Проверки без классификации",
        "group_source_count": "Записей в источнике",
        "group_projected_count": "Записей в модели",
        "coverage_details": "Детали покрытия",
        "unreported_groups": "Счётчики не указаны для",
        "input_diagnostics": "Замечания к входным данным",
        "crawl_invalid_reason_label": "Причина ошибки обхода",
        "crawl_finish_reason_label": "Причина завершения обхода",
        "crawl_stopped_reason_label": "Причина остановки обхода",
        "report_footer": "Технический аудит",
        "domain": "Сайт",
        "status_code_label": "HTTP-статус",
        "occurrences_count_label": "Число повторений",
        "occurrence_count_label": "Число повторений",
        "fix_hint_label": "Рекомендация",
        "remediation_status_label": "Статус исправления",
        "verification_status_label": "Статус проверки",
        "details_label": "Детали свидетельства",
        "locations_label": "Места обнаружения",
        "evidence_status": "Статус",
        "evidence_reason": "Причина",
        "evidence_id": "Идентификатор свидетельства",
        "evidence_source_table": "Таблица источника",
        "evidence_observation_id": "Идентификатор наблюдения",
        "evidence_role": "Роль",
        "pages_checked_label": "Проверено страниц",
        "findings_total_label": "Всего проблем",
        "findings_by_severity_label": "Проблемы по важности",
        "urls_crawled_label": "Обойдено URL",
        "scope_reason_label": "Причина ограничения",
        "operation_label": "Операция",
        "operation_bounded_site_audit": "Ограниченный аудит сайта",
        "tools_run": "Запущенные инструменты",
        "tools_failed": "Инструменты с ошибкой",
        "page_tools_failed": "Ошибки инструментов на страницах",
        "evidence_contract": "Контракт свидетельств",
        "capability_rows": "Статусы возможностей",
        "scan_identity_state": "Статус идентификатора обхода",
        "scan_uuid": "Идентификатор обхода",
        "checks_total": "Всего проверок",
        "failed_pages": "Страниц с ошибками",
        "pages_checked": "Проверено страниц",
        "tool": "Инструмент",
        "error": "Ошибка",
        "population": "Охват",
        "by_severity": "По важности",
        "health_score": "Оценка здоровья",
        "health_score_scope": "Охват оценки здоровья",
        "severity_note": "Примечание о важности",
        "title_element_is_missing": "Отсутствует заголовок страницы",
        "audit_finding": "Проблема аудита",
        "evidence_reason_no_stable": "В этом аудите нет стабильной ссылки на сохранённое свидетельство",
        "evidence_reason_unavailable": "Сохранённое свидетельство недоступно",
        "evidence_reason_incomplete": "Ссылка на сохранённое свидетельство неполна",
        "capability_records_below": "Записи о возможностях приведены ниже",
        "index_label": "Индекс",
    },
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _escape(value: Any, fallback: str = "") -> str:
    return html.escape(fallback if value is None else str(value), quote=True)


def _count(value: Any, lang: str) -> str:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return _LABELS[lang]["not_reported"]
    integer = int(value)
    return f"{integer:,}" if lang == "en" else f"{integer:,}".replace(",", "\u202f")


def _field_rows(value: Any, lang: str) -> list[tuple[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    rows: list[tuple[str, Any]] = []

    def flatten(key: str, item: Any) -> None:
        if item is None or item == "":
            return
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                flatten(f"{key}.{child_key}" if key else str(child_key), child)
        elif isinstance(item, list):
            if not item:
                return
            for index, child in enumerate(item, start=1):
                flatten(f"{key} {index}", child)
        elif isinstance(item, (str, int, float, bool)):
            title_parts = []
            for part in key.split("."):
                indexed = re.fullmatch(r"(.+?) (\d+)", part)
                if indexed:
                    suffix = f" #{indexed.group(2)}" if lang == "en" else f" №{indexed.group(2)}"
                    title_parts.append(_key_title(indexed.group(1), lang) + suffix)
                else:
                    title_parts.append(_key_title(part, lang))
            title = " · ".join(title_parts)
            rows.append((title, str(item)))

    for key, item in value.items():
        flatten(str(key), item)
    return rows


def _readable_record(value: Any, lang: str) -> str:
    """Flatten structured source evidence into labeled text, never JSON blobs."""
    if isinstance(value, Mapping):
        rows = _field_rows(value, lang)
        return "; ".join(f"{key}: {item}" for key, item in rows)
    if isinstance(value, list):
        return "; ".join(_readable_record(item, lang) for item in value if item is not None)
    return str(value) if value is not None else ""


def _reference_text(value: Any, lang: str) -> str:
    source_ref = _mapping(value)
    if source_ref.get("pointer"):
        return str(source_ref["pointer"])
    bits = [source_ref.get("collection")] if source_ref.get("collection") else []
    if source_ref.get("index") is not None:
        bits.append(f"{_LABELS[lang]['index_label']}: {source_ref['index']}")
    return "; ".join(str(bit) for bit in bits)


def _localize_generated_text(value: Any, *, lang: str, check: Any = None) -> Any:
    if not isinstance(value, str) or lang != "ru":
        return value
    labels = _LABELS[lang]
    if value == "Audit finding":
        return labels["audit_finding"]
    if value == "Title element is missing" or check == "TITLE_MISSING":
        return labels["title_element_is_missing"]
    evidence_reasons = {
        "no stable saved-evidence reference is present in this audit": "evidence_reason_no_stable",
        "saved evidence is unavailable": "evidence_reason_unavailable",
        "saved evidence reference is incomplete": "evidence_reason_incomplete",
    }
    key = evidence_reasons.get(value)
    return labels[key] if key else value


def _key_title(value: Any, lang: str | None = None) -> str:
    text = str(value or "").replace("_", " ").strip()
    localized = _LABELS.get(lang or "", {}).get(str(value or "").lower()) or _LABELS.get(
        lang or "", {}
    ).get(f"{str(value or '').lower()}_label")
    if localized:
        return localized
    return text[:1].upper() + text[1:] if text else ""


def _state_label(value: Any, lang: str) -> str:
    state = str(value or "unknown").lower()
    labels = _LABELS[lang]
    states = {
        "complete": labels["complete"],
        "partial": labels["partial"],
        "failed": labels["failed"],
        "unknown": labels["unknown"],
        "not_requested": labels["not_requested"],
        "recorded": labels["recorded"],
        "reported": labels["recorded"],
        "unavailable": labels["unavailable"],
        "measured": "Measured" if lang == "en" else "Измерено",
        "absent": "Absent" if lang == "en" else "Отсутствует",
        "skipped": labels["skipped"],
        "disabled": labels["disabled"],
        "not_run": "Not run" if lang == "en" else "Не запускалось",
        "run": "Run" if lang == "en" else "Запуск",
        "ran": "Ran" if lang == "en" else "Выполнена",
        "stale": "Stale" if lang == "en" else "Устарело",
        "not_applicable": "Not applicable" if lang == "en" else "Не применимо",
        "open": "Open" if lang == "en" else "Открыто",
        "verified": "Verified" if lang == "en" else "Проверено",
        "imported_projection": "Imported evidence projection"
        if lang == "en"
        else "Импортированная проекция свидетельства",
    }
    return states.get(state, str(value or labels["unknown"]))


def _severity_label(value: Any, lang: str) -> str:
    labels = _LABELS[lang]
    return {
        "critical": labels["critical"],
        "warning": labels["warning"],
        "notice": labels["notice"],
    }.get(str(value or "").lower(), str(value or labels["not_reported"]))


def _evidence_reference_rows(value: Mapping[str, Any], lang: str) -> list[tuple[str, Any]]:
    labels = _LABELS[lang]
    fields = (
        ("state", labels["evidence_status"]),
        ("reason", labels["evidence_reason"]),
        ("id", labels["evidence_id"]),
        ("source_table", labels["evidence_source_table"]),
        ("observation_id", labels["evidence_observation_id"]),
        ("role", labels["evidence_role"]),
    )
    rows = []
    for key, title in fields:
        if key not in value or value[key] is None:
            continue
        content = (
            _state_label(value[key], lang)
            if key == "state"
            else _localize_generated_text(value[key], lang=lang)
            if key == "reason"
            else value[key]
        )
        rows.append((title, content))
    for key, content in sorted(value.items()):
        if key in {name for name, _title in fields} or content is None:
            continue
        if isinstance(content, (Mapping, list)):
            content = _readable_record(content, lang)
        rows.append((_key_title(key, lang), content))
    return rows


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], *, lang: str) -> str:
    header_html = "".join(f'<th scope="col">{_escape(label)}</th>' for label in headers)
    if not rows:
        return f'<p class="empty">{_escape(_LABELS[lang]["none"])}</p>'
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_escape(value)}</td>" for value in row) + "</tr>" for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table></div>'


def _chapter(number: str, title: str, *, kicker: str = "") -> str:
    return (
        '<div class="chapter">'
        f'<span class="chapter-number">{_escape(number)}</span>'
        "<div>"
        + (f'<p class="kicker">{_escape(kicker)}</p>' if kicker else "")
        + f"<h2>{_escape(title)}</h2></div></div>"
    )


def _status(run: Mapping[str, Any], lang: str) -> str:
    labels = _LABELS[lang]
    value = run.get("state")
    allowed = {"complete", "partial", "failed", "unknown"}
    state = value if value in allowed else "unknown"
    reasons = run.get("reasons") if isinstance(run.get("reasons"), list) else []
    reason_html = "".join(f"<li>{_escape(_reason_text(reason, lang))}</li>" for reason in reasons)
    scope = run.get("scope")
    scope_text = _scope_text(scope, lang)
    note = {
        "complete": labels["complete_note"],
        "partial": labels["partial_warning"],
        "failed": labels["failed_warning"],
        "unknown": labels["unknown_warning"],
    }[state]
    return (
        f'<aside class="run-state state-{state}" data-state="{state}">'
        f'<div class="state-label">{_escape(labels["state"])} · '
        f"{_escape(labels[state])}</div>"
        + (f'<p class="state-note">{_escape(note)}</p>' if note else "")
        + f"<p><b>{_escape(labels['scope'])}:</b> {_escape(scope_text)}</p>"
        + (f"<ul>{reason_html}</ul>" if reason_html else "")
        + "</aside>"
    )


def _reason_text(reason: Any, lang: str) -> Any:
    if isinstance(reason, Mapping):
        field = reason.get("source_field")
        value = reason.get("value")
        label = _LABELS[lang].get(f"{field}_label") if isinstance(field, str) else None
        if label and value is not None:
            return f"{label}: {value}"
        return value if value is not None else reason
    return reason


def _scope_text(scope: Any, lang: str) -> Any:
    if scope in (None, "", {}, []):
        return _LABELS[lang]["no_reported_scope"]
    if isinstance(scope, Mapping):
        parts = []
        for key, value in scope.items():
            operation = _LABELS[lang].get(f"operation_{value}") if key == "operation" else None
            parts.append(f"{_key_title(key, lang)}: {operation or value}")
        return "; ".join(parts)
    if isinstance(scope, list):
        return "; ".join(str(item) for item in scope)
    return scope


def _count_value(section: Mapping[str, Any], key: str) -> Any:
    return _mapping(section.get(key)).get("projected_count")


def _declared_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("value") if value.get("state") == "reported" else None
    return value


def _summary_cards(summary: Mapping[str, Any], coverage: Mapping[str, Any], lang: str) -> str:
    labels = _LABELS[lang]
    counts = _mapping(summary.get("counts"))
    cards = []
    for key, label in (
        ("findings", labels["findings"]),
        ("pages", labels["pages"]),
        ("checks", labels["checks"]),
        ("backlog", labels["backlog"]),
    ):
        record = _mapping(counts.get(key))
        value = record.get("projected_count")
        if key == "checks":
            projected_checks = coverage.get("checks")
            value = (
                len(projected_checks)
                if coverage.get("state") == "reported" and isinstance(projected_checks, list)
                else None
            )
        source_count = record.get("source_count", record.get("source_total"))
        declared = _declared_value(record.get("declared_count", record.get("declared_total")))
        display_value = _count(value, lang)
        if key == "backlog" and record.get("state") in {"not_requested", "unavailable"}:
            display_value = labels[record["state"]]
        details = []
        if source_count is not None:
            details.append(f"{_escape(labels['source'])}: {_escape(_count(source_count, lang))}")
        if declared is not None and declared != source_count:
            detail_label = labels["check_inventory"] if key == "checks" else labels["declared"]
            details.append(f"{_escape(detail_label)}: {_escape(_count(declared, lang))}")
        cards.append(
            '<div class="metric-card">'
            f"<span>{_escape(label)}</span>"
            f"<strong>{_escape(display_value)}</strong>"
            f"<small>{' · '.join(details)}</small>"
            "</div>"
        )
    return '<div class="metric-grid">' + "".join(cards) + "</div>"


def _chart(
    title: str, labels: Sequence[str], values: Sequence[Any], *, lang: str, brand: Brand
) -> str:
    pairs = list(zip(labels, values, strict=True))
    if not pairs or any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for _label, value in pairs
    ):
        return (
            '<figure class="chart-card"><figcaption>'
            + _escape(title)
            + '</figcaption><p class="empty">'
            + _escape(_LABELS[lang]["not_reported"])
            + "</p></figure>"
        )
    chart = bar_chart(
        [label for label, _value in pairs],
        [
            Series(
                name=title,
                values=[value for _label, value in pairs],
                color=brand.accent,
                labels=True,
            )
        ],
        width=520,
        height=210,
        fmt=lambda value: _count(value, lang),
        ink=brand.ink,
        grid=brand.table_header,
        title=title,
    )
    return f'<figure class="chart-card"><figcaption>{_escape(title)}</figcaption>{chart}</figure>'


def _severity_chart(summary: Mapping[str, Any], *, lang: str, brand: Brand) -> str:
    source = _mapping(summary.get("source"))
    counts = next(
        (
            _mapping(source.get(key))
            for key in ("findings_by_severity", "severity_counts", "by_severity")
            if isinstance(source.get(key), Mapping)
        ),
        {},
    )
    labels = _LABELS[lang]
    values = [counts.get(key) for key in ("critical", "warning", "notice")]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        return (
            f'<figure class="chart-card"><figcaption>{_escape(labels["findings_by_severity"])}</figcaption>'
            f'<p class="empty">{_escape(labels["no_severity_data"])}</p></figure>'
        )
    return _chart(
        labels["findings_by_severity"],
        [labels[key] for key in ("critical", "warning", "notice")],
        values,
        lang=lang,
        brand=brand,
    )


def _coverage_state(item: Any) -> Any:
    check = _mapping(item)
    state = check.get("state")
    record = _mapping(check.get("record"))
    if state is None or state in ("unreported", "source_reported"):
        return record.get("state") or state
    return state


def _checks_chart(
    summary: Mapping[str, Any],
    coverage: Mapping[str, Any],
    source_kind: Any,
    *,
    lang: str,
    brand: Brand,
) -> str:
    counts = _mapping(_mapping(summary.get("counts")).get("checks"))
    labels = _LABELS[lang]
    check_records = coverage.get("checks")
    capability_group = _mapping(counts.get("capabilities"))
    if (
        capability_group.get("state") == "reported"
        and isinstance(check_records, list)
        and check_records
    ):
        capabilities = [
            item
            for item in check_records
            if _mapping(_mapping(item).get("source_ref")).get("collection")
            == "summary.evidence_contract.capability_rows"
        ]
        status_counts: dict[str, int] = {}
        for item in capabilities:
            state = _coverage_state(item)
            if isinstance(state, str):
                status_counts[state] = status_counts.get(state, 0) + 1
        if status_counts:
            statuses = [
                "measured",
                "partial",
                "absent",
                "unavailable",
                "not_requested",
                "unreported",
            ]
            chart_labels = [
                _state_label(state, lang) for state in statuses if state in status_counts
            ]
            values = [status_counts[state] for state in statuses if state in status_counts]
            return _chart(
                labels["capabilities_by_state"],
                chart_labels,
                values,
                lang=lang,
                brand=brand,
            )
    fields = (
        (
            "ran",
            labels["group_fired"] if source_kind == "sf-audit" else labels["ran"],
        ),
        ("silent", labels["group_silent"]),
        ("failed", labels["failed_checks"]),
        ("skipped", labels["skipped"]),
        ("disabled", labels["disabled"]),
        ("page_tools_failed", labels["group_page_tools_failed"]),
    )
    available = []
    unavailable = []
    for key, label in fields:
        group = _mapping(counts.get(key))
        value = group.get("projected_count")
        if (
            group.get("state") == "reported"
            and type(value) in (int, float)
            and math.isfinite(value)
            and value >= 0
        ):
            available.append((key, label, value))
        else:
            unavailable.append(label)
    # Older models may omit the nested group block while retaining the per-check
    # rows. Count only explicitly recorded states; a missing group never becomes zero.
    if not available and coverage.get("state") == "reported":
        records = check_records
        if isinstance(records, list):
            statuses: dict[str, int] = {}
            for item in records:
                state = _coverage_state(item)
                if isinstance(state, str):
                    statuses[state] = statuses.get(state, 0) + 1
            for key, label in (
                ("measured", labels["ran"]),
                ("failed", labels["failed_checks"]),
                ("skipped", labels["skipped"]),
                ("disabled", labels["disabled"]),
                ("unavailable", labels["unavailable"]),
                ("not_requested", labels["not_requested"]),
                ("partial", labels["partial"]),
            ):
                if key in statuses:
                    available.append((key, label, statuses[key]))
    if not available:
        return (
            f'<figure class="chart-card"><figcaption>{_escape(labels["checks_by_state"])}</figcaption>'
            f'<p class="empty">{_escape(labels["no_check_counts"])}</p></figure>'
        )
    chart = _chart(
        labels["checks_by_state"],
        [label for _key, label, _value in available],
        [value for _key, _label, value in available],
        lang=lang,
        brand=brand,
    )
    missing = (
        f'<p class="empty">{_escape(labels["unreported_groups"])}: '
        + ", ".join(_escape(label) for label in unavailable)
        + "</p>"
        if unavailable
        else ""
    )
    return chart.replace("</figure>", missing + "</figure>")


def _finding_cards(findings: Sequence[Any], *, lang: str) -> str:
    labels = _LABELS[lang]
    output = []
    for index, value in enumerate(findings, start=1):
        finding = _mapping(value)
        display = _mapping(finding.get("display"))
        record = _mapping(finding.get("record"))
        severity = record.get("severity") or display.get("severity")
        severity_label = _severity_label(severity, lang)
        check = display.get("check_key") or record.get("check") or record.get("source") or ""
        title = display.get("title") or record.get("title") or labels["finding"]
        if title == "Audit finding" or check == "TITLE_MISSING":
            title = _localize_generated_text(title, lang=lang, check=check)
        observation = display.get("observation")
        reproduction = display.get("reproduction")
        if isinstance(reproduction, str) and lang == "ru" and reproduction.startswith("At "):
            reproduction = "На " + reproduction[3:]
        url = record.get("url")
        source_ref = _mapping(finding.get("source_ref"))
        source_id = source_ref.get("id") or source_ref.get("id_if_present")
        parts = [
            f'<article class="finding-card severity-{_escape(str(severity).lower())}">',
            '<div class="finding-heading">',
            f'<span class="finding-number">{index:02d}</span>',
            f'<div><p class="finding-meta">{_escape(severity_label)}'
            + (f" · {_escape(check)}" if check else "")
            + (f" · {_escape(source_id)}" if source_id else "")
            + "</p>"
            + f"<h3>{_escape(title)}</h3></div></div>",
        ]
        if observation:
            parts.append(f"<p><b>{_escape(labels['observation'])}:</b> {_escape(observation)}</p>")
        if reproduction:
            parts.append(
                f"<p><b>{_escape(labels['reproduction'])}:</b> {_escape(reproduction)}</p>"
            )
        if url:
            parts.append(f'<p class="url"><b>{_escape(labels["page_url"])}:</b> {_escape(url)}</p>')
        for key in (
            "status_code",
            "occurrences_count",
            "occurrence_count",
            "fix_hint",
            "remediation_status",
            "verification_status",
        ):
            if record.get(key) not in (None, "", [], {}):
                rendered = (
                    _readable_record(record[key], lang)
                    if isinstance(record[key], (Mapping, list))
                    else str(record[key])
                )
                if key.endswith("_status") or key == "status":
                    rendered = _state_label(rendered, lang)
                parts.append(
                    f'<p class="evidence-extra"><b>{_escape(_key_title(key, lang))}:</b> '
                    f"{_escape(rendered)}</p>"
                )
        for key in ("details", "locations"):
            label = labels["details_label"] if key == "details" else labels["locations_label"]
            display_values = display.get(key)
            if isinstance(display_values, list) and display_values:
                for detail in display_values:
                    parts.append(
                        f'<p class="evidence-extra"><b>{_escape(label)}:</b> {_escape(detail)}</p>'
                    )
                continue
            raw_value = record.get(key)
            if raw_value not in (None, "", [], {}):
                rendered = (
                    _readable_record(raw_value, lang)
                    if isinstance(raw_value, (Mapping, list))
                    else str(raw_value)
                )
                parts.append(
                    f'<p class="evidence-extra"><b>{_escape(label)}:</b> {_escape(rendered)}</p>'
                )
        evidence_ref = display.get("evidence_reference")
        if isinstance(evidence_ref, Mapping) and evidence_ref:
            reference = "; ".join(
                f"{label}: {value}" for label, value in _evidence_reference_rows(evidence_ref, lang)
            )
            parts.append(
                f'<p class="evidence-extra"><b>{_escape(labels["source"])}:</b> '
                f"{_escape(reference)}</p>"
            )
        parts.append("</article>")
        output.append("".join(parts))
    return "".join(output) if output else f'<p class="empty">{_escape(labels["none"])}</p>'


def _page_rows(pages: Sequence[Any]) -> list[list[Any]]:
    rows = []
    for value in pages:
        page = _mapping(value)
        record = _mapping(page.get("record"))
        rows.append(
            [
                record.get("url"),
                record.get("status", record.get("status_code")),
                record.get("title"),
            ]
        )
    return rows


def _coverage_source_details(
    section: str, record: Any, coverage: Mapping[str, Any], lang: str
) -> str:
    if section != "source_evidence" or not isinstance(record, Mapping):
        return _readable_record(record, lang)

    capability_checks = [
        check
        for check in coverage.get("checks", [])
        if _mapping(_mapping(check).get("source_ref")).get("collection")
        == "summary.evidence_contract.capability_rows"
    ]
    projected_capability_records = [_mapping(check).get("record") for check in capability_checks]

    def remove_projected_capabilities(value: Any) -> tuple[Any, int]:
        if isinstance(value, Mapping):
            result = {}
            removed = 0
            for key, item in value.items():
                if (
                    key == "capability_rows"
                    and isinstance(item, list)
                    and projected_capability_records == item
                ):
                    removed += len(item)
                    continue
                clean, child_removed = remove_projected_capabilities(item)
                result[key] = clean
                removed += child_removed
            return result, removed
        if isinstance(value, list):
            result = []
            removed = 0
            for item in value:
                clean, child_removed = remove_projected_capabilities(item)
                result.append(clean)
                removed += child_removed
            return result, removed
        return value, 0

    clean_record, removed_count = remove_projected_capabilities(record)
    parts = []
    readable = _readable_record(clean_record, lang)
    if readable:
        parts.append(readable)
    if removed_count:
        parts.append(f"{removed_count} {_LABELS[lang]['capability_records_below']}")
    return "; ".join(parts)


def _coverage_rows(coverage: Mapping[str, Any], lang: str) -> list[list[Any]]:
    labels = _LABELS[lang]
    rows = []
    for section, label_key in (
        ("source_evidence", "source"),
        ("source_check_coverage", "source_check_coverage"),
    ):
        evidence = coverage.get(section)
        if not isinstance(evidence, Mapping):
            continue
        state = evidence.get("state")
        record = evidence.get("record")
        if record is None:
            details = labels["unavailable"] if state == "unavailable" else labels["not_reported"]
        elif isinstance(record, (Mapping, list)):
            details = _coverage_source_details(section, record, coverage, lang)
        else:
            details = str(record)
        reference = _reference_text(evidence.get("source_ref"), lang)
        if reference:
            ref_label = "Source reference" if lang == "en" else "Ссылка на источник"
            details = (
                f"{details}; {ref_label}: {reference}" if details else f"{ref_label}: {reference}"
            )
        rows.append(
            [
                labels[label_key],
                _state_label(state, lang) if state else labels["not_reported"],
                details,
            ]
        )

    groups = _mapping(coverage.get("groups"))
    for name, value in groups.items():
        group = _mapping(value)
        label = labels.get(f"group_{name}", _key_title(name, lang))
        source_count = group.get("source_count")
        projected_count = group.get("projected_count")
        details = (
            f"{labels['group_source_count']}: {_count(source_count, lang)}; "
            f"{labels['group_projected_count']}: {_count(projected_count, lang)}"
        )
        state = group.get("state")
        rows.append(
            [label, _state_label(state, lang) if state else labels["not_reported"], details]
        )

    for value in coverage.get("checks") or []:
        check = _mapping(value)
        record_value = check.get("record")
        record = _mapping(record_value)
        display = _mapping(check.get("display"))
        source_ref = _mapping(check.get("source_ref"))
        key = (
            check.get("id")
            or display.get("title")
            or record.get("check")
            or record.get("id")
            or record.get("tool")
            or record.get("name")
            or source_ref.get("id")
            or source_ref.get("pointer")
            or labels["check"]
        )
        raw_state = check.get("state")
        record_state = record.get("state")
        if raw_state in {None, "unreported", "source_reported"} and isinstance(record_state, str):
            raw_state = record_state
        state = _state_label(raw_state, lang) if raw_state else labels["not_reported"]
        reason = check.get("reason") or record.get("reason") or record.get("error")
        reason = _localize_generated_text(reason, lang=lang)
        detail_fields = {
            field: item
            for field, item in record.items()
            if field not in {"id", "name", "check", "tool", "state", "reason", "error"}
        }
        if detail_fields:
            detail_text = _readable_record(detail_fields, lang)
            if record.get("failed_pages") is not None and record.get("pages_checked") is not None:
                failed = _count(record["failed_pages"], lang)
                checked = _count(record["pages_checked"], lang)
                detail_text = (
                    f"Errors on {failed} of {checked} pages"
                    if lang == "en"
                    else f"Ошибки на {failed} из {checked} страниц"
                )
            reason = f"{reason}; {detail_text}" if reason else detail_text
        elif isinstance(record_value, str) and not reason:
            reason = record_value
        reference = _reference_text(source_ref, lang)
        if reference:
            ref_label = "Source reference" if lang == "en" else "Ссылка на источник"
            reason = (
                f"{reason}; {ref_label}: {reference}" if reason else f"{ref_label}: {reference}"
            )
        rows.append([key, state, reason])
    return rows


def _backlog_rows(backlog: Mapping[str, Any], lang: str) -> list[list[Any]]:
    labels = _LABELS[lang]
    rows = []
    for value in backlog.get("items") or []:
        wrapper = _mapping(value)
        item = _mapping(wrapper.get("record", wrapper))
        source_ref = _mapping(wrapper.get("source_ref"))
        reference = source_ref.get("id") or source_ref.get("pointer") or item.get("id")
        title = item.get("title") or item.get("name") or item.get("id") or labels["item"]
        raw_state = item.get("state") or item.get("attempt_status") or item.get("status")
        raw_verification = item.get("verification") or item.get("verification_status")
        state = _state_label(raw_state, lang) if raw_state else labels["not_reported"]
        verification = (
            _state_label(raw_verification, lang) if raw_verification else labels["not_reported"]
        )
        reason = item.get("reason") or item.get("blocked_by")
        rows.append([reference or labels["not_reported"], title, state, verification, reason])
    return rows


def _omission_rows(omissions: Any) -> list[list[Any]]:
    rows = []
    for value in omissions or []:
        omission = _mapping(value)
        rows.append(
            [
                omission.get("collection") or omission.get("field"),
                omission.get("count"),
                omission.get("reason"),
            ]
        )
    return rows


def _styles(brand: Brand, lang: str) -> str:
    footer = _LABELS[lang]["report_footer"].replace("\\", "\\\\").replace('"', '\\"')
    return f"""
@page {{
  size: 13.333in 7.5in;
  margin: 15mm 17mm 18mm;
  @bottom-left {{ content: "{footer}"; color: {brand.ink}; font: 8pt {brand.font_stack}; }}
  @bottom-right {{ content: counter(page); color: {brand.accent}; font: 8pt {brand.font_stack}; }}
}}
* {{ box-sizing: border-box; }}
html {{ color: {brand.ink}; font-family: {brand.font_stack}; font-size: 10pt; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; line-height: 1.45; }}
header {{ border-bottom: 1px solid {brand.table_header}; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 0 0 8px; margin: 0 0 18px; color: {brand.ink}; }}
.brand {{ display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: .04em; }}
.brand-mark {{ display: inline-flex; align-items: end; gap: 2px; height: 16px; }}
.brand-mark i {{ display: block; width: 4px; border-radius: 2px; background: {brand.accent}; }}
.brand-mark i:nth-child(1) {{ height: 7px; }} .brand-mark i:nth-child(2) {{ height: 11px; }} .brand-mark i:nth-child(3) {{ height: 16px; }}
.header-site {{ color: #64748B; font-size: 9pt; text-align: right; overflow-wrap: anywhere; }}
h1 {{ font-size: 26pt; line-height: 1.08; letter-spacing: -.02em; margin: 5px 0 4px; }}
h2 {{ font-size: 19pt; line-height: 1.15; margin: 0; }} h3 {{ font-size: 12pt; line-height: 1.25; margin: 2px 0 7px; }}
p {{ margin: 5px 0; }} .subtitle {{ color: #64748B; font-size: 11pt; margin: 0 0 16px; }}
.kicker {{ color: {brand.accent}; font-size: 8pt; font-weight: 700; letter-spacing: .1em; margin: 0 0 4px; text-transform: uppercase; }}
.run-state {{ border-left: 4px solid {brand.accent}; background: {brand.card}; padding: 10px 13px; margin: 10px 0 14px; break-inside: avoid; }}
.state-partial,.state-failed,.state-unknown {{ border-left-color: {brand.negative}; background: #FFF7F4; }}
.state-label {{ font-weight: 700; font-size: 10pt; }} .state-note {{ font-weight: 600; }}
.run-state ul {{ margin: 5px 0 0; padding-left: 18px; }}
.metric-grid {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 9px; margin: 14px 0; }}
.metric-card {{ min-height: 72px; border: 1px solid {brand.table_header}; border-top: 3px solid {brand.accent}; background: {brand.card}; padding: 9px 11px; break-inside: avoid; }}
.metric-card span,.metric-card small {{ display: block; color: #64748B; font-size: 8pt; }} .metric-card strong {{ display:block; font-size: 20pt; line-height: 1.15; margin: 4px 0; }} .metric-card small {{ min-height: 10px; }}
.chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0; }}
.chart-card {{ border: 1px solid {brand.table_header}; border-radius: 5px; padding: 8px 10px; margin: 0; break-inside: avoid; }}
.chart-card figcaption {{ font-size: 9pt; font-weight: 700; margin-bottom: 4px; }} .chart-card svg {{ display:block; width:100%; height:auto; max-height: 155px; }}
.chart-legend {{ display:flex; flex-wrap:wrap; gap: 4px 12px; list-style:none; padding:0; margin:0; font-size:7pt; color:#475569; }} .chart-legend li {{ display:flex; gap:5px; align-items:center; }} .chart-legend i {{ display:inline-block; width:7px; height:7px; border-radius:50%; }}
.chapter {{ display:flex; align-items:center; gap:11px; border-bottom:1px solid {brand.table_header}; padding:0 0 10px; margin:0 0 11px; break-after:avoid; }}
.chapter-number {{ display:grid; place-items:center; flex:none; width:34px; height:34px; border-radius:50%; background:{brand.accent}; color:#fff; font-weight:700; font-size:10pt; }}
.chapter h2 {{ font-size:17pt; }}
.section {{ margin: 0 0 14px; }} .section-start {{ break-before: page; }}
.finding-card {{ border:1px solid {brand.table_header}; border-left:4px solid {brand.accent}; border-radius:4px; padding:9px 11px; margin:0 0 8px; break-inside:avoid; overflow-wrap:anywhere; }}
.finding-card.severity-critical {{ border-left-color:{brand.negative}; }} .finding-card.severity-warning {{ border-left-color:#E58A13; }}
.finding-heading {{ display:flex; gap:10px; align-items:flex-start; }} .finding-number {{ font-size:16pt; line-height:1; color:{brand.accent}; font-weight:700; }}
.finding-meta {{ color:#64748B; font-size:7.5pt; font-weight:700; letter-spacing:.04em; margin:0; text-transform:uppercase; }}
.url {{ color:#475569; overflow-wrap:anywhere; }}
.table-wrap {{ width:100%; overflow:visible; }} table {{ width:100%; border-collapse:collapse; table-layout:fixed; font-size:8pt; }}
thead {{ display:table-header-group; }} tr {{ break-inside:avoid; }} th {{ text-align:left; color:{brand.ink}; background:{brand.table_header}; font-size:7.5pt; padding:7px 8px; }}
td {{ border-bottom:1px solid #E8EDF4; padding:7px 8px; vertical-align:top; overflow-wrap:anywhere; }} tr:nth-child(even) td {{ background:#FAFBFD; }}
.empty {{ color:#64748B; font-style:italic; padding:8px 0; }} .notes {{ border-left:3px solid #E58A13; background:#FFF9ED; padding:8px 11px; break-inside:avoid; }}
.muted {{ color:#64748B; }} .identity {{ font-size:10pt; margin-bottom:10px; }}
"""


def render_audit_pdf_html(model: Mapping[str, Any], *, lang: str = "en", brand: Any = None) -> str:
    """Render one versioned audit PDF model into offline, print-ready localized HTML."""
    if lang not in _LABELS:
        raise ValueError(f"unsupported report language {lang!r}; expected one of {tuple(_LABELS)}")
    if not isinstance(model, Mapping) or model.get("schema") != _MODEL_SCHEMA:
        raise ValueError(f"audit PDF model must declare schema {_MODEL_SCHEMA!r}")

    labels = _LABELS[lang]
    source = _mapping(model.get("source"))
    run = _mapping(model.get("run"))
    summary = _mapping(model.get("summary"))
    coverage = _mapping(model.get("coverage"))
    backlog = _mapping(model.get("backlog"))
    findings = model.get("findings") if isinstance(model.get("findings"), list) else []
    pages = model.get("pages") if isinstance(model.get("pages"), list) else []
    loaded_brand = load_brand(brand)
    title = labels["report"]
    domain = source.get("domain") or source.get("url") or labels["not_reported"]
    generated = source.get("generated_at")
    source_kind = source.get("kind")
    source_label = (
        labels.get(f"source_{source_kind.replace('-', '_')}")
        if isinstance(source_kind, str)
        else None
    )
    source_label = source_label or source_kind or source.get("schema") or labels["not_reported"]

    head = (
        '<!doctype html><html lang="'
        + lang
        + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        + f"<title>{_escape(title)} · {_escape(domain)}</title><style>{_styles(loaded_brand, lang)}</style></head><body>"
    )
    header = (
        '<header><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>'
        + f"<span>{_escape(loaded_brand.name or labels['report'])}</span>"
        + (
            f'<span class="muted">{_escape(loaded_brand.logo_text)}</span>'
            if loaded_brand.logo_text
            else ""
        )
        + f'</div><div class="header-site">{_escape(domain)}</div></header>'
    )

    summary_source = _mapping(summary.get("source"))
    structured_summary_keys = {
        "findings_by_severity",
        "tools_run",
        "tools_failed",
        "page_tools_failed",
        "checks_disabled",
        "evidence_contract",
        "check_coverage",
        "project_coverage",
        "pages_checked",
        "urls_crawled",
        "findings_total",
        "issues_total",
    }
    summary_rows = _field_rows(
        {key: value for key, value in summary_source.items() if key not in structured_summary_keys},
        lang,
    )
    diagnostics = source.get("input_diagnostics")
    if diagnostics:
        summary_rows.append(
            (
                labels["input_diagnostics"],
                _readable_record(diagnostics, lang),
            )
        )
    summary_table = (
        f'<section class="section">{_table([labels["summary_field"], labels["state_col"]], summary_rows, lang=lang)}</section>'
        if summary_rows
        else ""
    )

    intro = (
        "<main>"
        + f'<p class="kicker">{_escape(source_label)}</p>'
        + f"<h1>{_escape(title)}</h1>"
        + f'<p class="subtitle"><b>{_escape(labels["audit_for"])}:</b> {_escape(domain)}'
        + (f" · <b>{_escape(labels['prepared'])}:</b> {_escape(generated)}" if generated else "")
        + "</p>"
        + _status(run, lang)
        + _summary_cards(summary, coverage, lang)
        + '<div class="chart-grid">'
        + _severity_chart(summary, lang=lang, brand=loaded_brand)
        + _checks_chart(summary, coverage, source.get("kind"), lang=lang, brand=loaded_brand)
        + "</div>"
        + summary_table
        + "</main>"
    )

    coverage_section = (
        '<section class="section section-start"><div class="chapter">'
        + '<span class="chapter-number">01</span><div>'
        + f'<p class="kicker">{_escape(labels["source"])}</p><h2>{_escape(labels["coverage"])}</h2></div></div>'
        + f'<p class="muted">{_escape(labels["coverage_reported"] if coverage.get("state") == "reported" else _state_label(coverage.get("state"), lang) if coverage.get("state") else labels["unavailable"])}</p>'
        + _table(
            [labels["check"], labels["state_col"], labels["reason"]],
            _coverage_rows(coverage, lang),
            lang=lang,
        )
        + "</section>"
    )

    findings_section = (
        f'<section class="section section-start">{_chapter("02", labels["findings"])}'
        + f'<p class="muted">{_escape(_count(_count_value(_mapping(summary.get("counts")), "findings"), lang))} · '
        + f"{_escape(labels['state_col'])}: {_escape(_state_label(run.get('state'), lang))}</p>"
        + _finding_cards(findings, lang=lang)
        + "</section>"
    )

    pages_section = (
        f'<section class="section section-start">{_chapter("03", labels["pages"])}'
        + _table(
            [labels["page_url"], labels["status_code"], labels["title"]],
            _page_rows(pages),
            lang=lang,
        )
        + "</section>"
    )

    backlog_state = _state_label(backlog.get("state"), lang)
    backlog_section = (
        f'<section class="section section-start">{_chapter("04", labels["backlog"])}'
        + f'<p class="muted">{_escape(backlog_state)}</p>'
        + _table(
            [
                labels["reference"],
                labels["item"],
                labels["state_col"],
                labels["verification"],
                labels["reason"],
            ],
            _backlog_rows(backlog, lang),
            lang=lang,
        )
        + "</section>"
    )

    omission_rows = _omission_rows(model.get("omissions"))
    omissions_section = ""
    if omission_rows:
        omissions_section = (
            f'<section class="section">{_chapter("05", labels["omissions"])}'
            + _table([labels["item"], labels["count"], labels["reason"]], omission_rows, lang=lang)
            + "</section>"
        )

    footer = "</body></html>"
    return (
        head
        + header
        + intro
        + coverage_section
        + findings_section
        + pages_section
        + backlog_section
        + omissions_section
        + footer
    )

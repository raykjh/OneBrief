"""Create a human-readable XLSX companion for every completed result."""

from __future__ import annotations

import re
from pathlib import Path

import xlsxwriter

from onebrief.public_research import PublicResearchResult


_TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def markdown_tables(markdown: str) -> list[list[list[str]]]:
    lines = markdown.splitlines()
    tables: list[list[list[str]]] = []
    index = 0
    while index + 1 < len(lines):
        if "|" not in lines[index] or not _TABLE_SEPARATOR.match(lines[index + 1]):
            index += 1
            continue
        rows = [_cells(lines[index])]
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            rows.append(_cells(lines[index]))
            index += 1
        width = len(rows[0])
        tables.append([row[:width] + [""] * max(0, width - len(row)) for row in rows])
    return tables


def export_workbook(
    final_markdown: str,
    destination: Path,
    public_research: PublicResearchResult | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(str(destination))
    header = workbook.add_format({"bold": True, "bg_color": "#DCEBE3", "border": 1})
    wrap = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1})
    link = workbook.add_format({"font_color": "blue", "underline": 1, "text_wrap": True})
    tables = markdown_tables(final_markdown)
    if not tables and public_research is not None:
        tables = markdown_tables(public_research.answer_markdown)

    if tables:
        for number, rows in enumerate(tables, start=1):
            sheet = workbook.add_worksheet("결과" if number == 1 else f"결과{number}")
            for column, value in enumerate(rows[0]):
                sheet.write(0, column, value, header)
            for row_index, row in enumerate(rows[1:], start=1):
                for column, value in enumerate(row):
                    if value.startswith(("http://", "https://")):
                        sheet.write_url(row_index, column, value, link, value)
                    else:
                        sheet.write(row_index, column, value, wrap)
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(0, len(rows) - 1), max(0, len(rows[0]) - 1))
            sheet.set_column(0, max(0, len(rows[0]) - 1), 22)
    else:
        sheet = workbook.add_worksheet("결과")
        sheet.write(0, 0, "OneBrief 결과", header)
        sheet.write(1, 0, final_markdown, wrap)
        sheet.set_column(0, 0, 100)
        sheet.set_row(1, 320)

    if public_research is not None:
        sources = workbook.add_worksheet("공개출처")
        for column, value in enumerate(["출처 ID", "제목", "도메인", "URL"]):
            sources.write(0, column, value, header)
        for row, source in enumerate(public_research.sources, start=1):
            sources.write(row, 0, source.source_id, wrap)
            sources.write(row, 1, source.title, wrap)
            sources.write(row, 2, source.domain, wrap)
            sources.write_url(row, 3, source.url, link, source.url)
        sources.freeze_panes(1, 0)
        sources.set_column(0, 0, 12)
        sources.set_column(1, 2, 30)
        sources.set_column(3, 3, 70)
    workbook.close()

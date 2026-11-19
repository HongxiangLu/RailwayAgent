"""读取规范 Excel 中与条款关联的表格和图片。

本模块只读源工作簿。图片会解压到 Flask 的 static 目录，供前端按 URL 展示；
表格转换为 Markdown 文本，便于 Neo4j 存储、全文检索和前端渲染。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path, PurePosixPath
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from openpyxl import load_workbook


TABLE_SHEET = "检验表格"
IMAGE_SHEET = "图片"
INVALID_NAMES = {"", "—", "-", "/"}
TABLE_BOUNDARY_RE = re.compile(r"^(?:说明)?(?:续\s*)?表(?:格)?\s*[A-Z0-9]", re.I)
TABLE_TITLE_RE = re.compile(
    r"^(?:说明)?(?:续\s*)?表(?:格)?\s*"
    r"([A-Z](?:\.\d+(?:\.[0-9IⅠ⊥]+)?)?(?:[—–-]\d+)?|\d+(?:\.\d+)+(?:[—–-]\d+)?|\d+)",
    re.I,
)
TABLE_MARKER_RE = re.compile(r"含表格[：:]\s*([^】\n]+)")
TABLE_REFERENCE_RE = re.compile(
    r"表(?:格)?\s*"
    r"([A-Z](?:\.\d+(?:\.[0-9IⅠ⊥]+)?)?(?:[—–-]\d+)?|\d+(?:\.\d+)+(?:[—–-]\d+)?|\d+)",
    re.I,
)
FIGURE_MARKER_RE = re.compile(r"含图片[：:]\s*([^】\n]+)")
FIGURE_REFERENCE_RE = re.compile(
    r"(?:说明)?图\s*([A-Z](?:\.\d+(?:\.\d+)?)?(?:[—–-]\d+)?|\d+(?:\.\d+)+(?:[—–-]\d+)?)",
    re.I,
)
PAGE_RE = re.compile(r"(?:扫描页|PDF\s*第?)\s*(\d+)", re.I)

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_DRAWING_MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"


def normalize_asset_number(value) -> str:
    number = str(value or "").strip().upper().replace(" ", "")
    number = number.replace("－", "-").replace("–", "—")
    number = re.sub(r"(?<=\.)[IⅠ⊥](?=$|[—-])", "1", number)
    return number


def base_asset_number(value) -> str:
    """图/表的分图后缀不属于条款号，如 D.0.1—1 对应 D.0.1。"""
    number = normalize_asset_number(value)
    if number.count(".") >= 2:
        return re.sub(r"[—-]\d+$", "", number)
    return number


def marker_numbers(content: str, marker_re, reference_re) -> list[str]:
    numbers = []
    for marker in marker_re.finditer(content or ""):
        for match in reference_re.finditer(marker.group(1)):
            numbers.append(normalize_asset_number(match.group(1)))
    return list(dict.fromkeys(numbers))


def table_marker_numbers(content: str) -> list[str]:
    return marker_numbers(content, TABLE_MARKER_RE, TABLE_REFERENCE_RE)


def figure_marker_numbers(content: str) -> list[str]:
    return marker_numbers(content, FIGURE_MARKER_RE, FIGURE_REFERENCE_RE)


def referenced_numbers(content: str, reference_re) -> list[str]:
    return list(dict.fromkeys(
        normalize_asset_number(match.group(1))
        for match in reference_re.finditer(content or "")
    ))


def _markdown_cell(value) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _vertical_merged_values(sheet) -> dict[tuple[int, int], object]:
    values = {}
    for merged in sheet.merged_cells.ranges:
        if merged.max_row <= merged.min_row:
            continue
        source = sheet.cell(merged.min_row, merged.min_col).value
        for row in range(merged.min_row + 1, merged.max_row + 1):
            values[(row, merged.min_col)] = source
    return values


def _table_markdown(sheet, start_row: int, end_row: int) -> str:
    inherited = _vertical_merged_values(sheet)
    rows = []
    for row_number in range(start_row, end_row + 1):
        values = []
        for column in range(1, sheet.max_column + 1):
            value = sheet.cell(row_number, column).value
            if value in (None, ""):
                value = inherited.get((row_number, column))
            values.append(_markdown_cell(value))
        while values and not values[-1]:
            values.pop()
        if any(values):
            rows.append(values)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    body = rows[1:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def read_tables(path: Path, standard_name: str) -> list[dict]:
    workbook = load_workbook(path, read_only=False, data_only=True)
    try:
        if TABLE_SHEET not in workbook.sheetnames:
            return []
        sheet = workbook[TABLE_SHEET]
        boundaries = []
        for row in range(1, sheet.max_row + 1):
            title = str(sheet.cell(row, 1).value or "").strip()
            if TABLE_BOUNDARY_RE.match(title):
                boundaries.append((row, title))

        records = []
        for position, (row, title) in enumerate(boundaries):
            match = TABLE_TITLE_RE.match(title)
            if not match:
                continue
            number = normalize_asset_number(match.group(1))
            end_row = boundaries[position + 1][0] - 1 if position + 1 < len(boundaries) else sheet.max_row
            content = _table_markdown(sheet, row + 1, end_row)
            if not content:
                continue
            page_match = PAGE_RE.search(title)
            records.append({
                "id": f"{standard_name}|{TABLE_SHEET}|{row}",
                "准则": standard_name,
                "条款号": "",
                "编号": number,
                "对应编号": base_asset_number(number),
                "标题": re.sub(r"（(?:扫描页|PDF)[^）]*）", "", title).strip(),
                "内容": content,
                "PDF页码": int(page_match.group(1)) if page_match else None,
                "来源文件": path.name,
                "工作表": TABLE_SHEET,
                "源行": row,
            })
        return records
    finally:
        workbook.close()


def _part_path(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _rels_path(part: str) -> str:
    parent = str(PurePosixPath(part).parent)
    name = PurePosixPath(part).name
    return posixpath.join(parent, "_rels", f"{name}.rels")


def _relationships(archive: zipfile.ZipFile, part: str) -> dict[str, str]:
    path = _rels_path(part)
    if path not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(path))
    return {
        item.attrib["Id"]: _part_path(part, item.attrib["Target"])
        for item in root.findall(f"{{{NS_PACKAGE_REL}}}Relationship")
    }


def _sheet_part(archive: zipfile.ZipFile, sheet_name: str) -> str | None:
    workbook_part = "xl/workbook.xml"
    root = ET.fromstring(archive.read(workbook_part))
    relationships = _relationships(archive, workbook_part)
    for sheet in root.findall(f".//{{{NS_MAIN}}}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            return relationships.get(sheet.attrib.get(f"{{{NS_REL}}}id"))
    return None


def _drawing_images(archive: zipfile.ZipFile, sheet_name: str) -> list[tuple[int, str]]:
    sheet_part = _sheet_part(archive, sheet_name)
    if not sheet_part:
        return []
    sheet_root = ET.fromstring(archive.read(sheet_part))
    sheet_rels = _relationships(archive, sheet_part)
    drawing = sheet_root.find(f"{{{NS_MAIN}}}drawing")
    if drawing is None:
        return []
    drawing_part = sheet_rels.get(drawing.attrib.get(f"{{{NS_REL}}}id"))
    if not drawing_part:
        return []
    drawing_root = ET.fromstring(archive.read(drawing_part))
    drawing_rels = _relationships(archive, drawing_part)
    result = []
    for anchor in list(drawing_root):
        from_node = anchor.find(f"{{{NS_DRAWING}}}from")
        blip = anchor.find(f".//{{{NS_DRAWING_MAIN}}}blip")
        if from_node is None or blip is None:
            continue
        row_node = from_node.find(f"{{{NS_DRAWING}}}row")
        relation_id = blip.attrib.get(f"{{{NS_REL}}}embed")
        if row_node is not None and relation_id in drawing_rels:
            result.append((int(row_node.text) + 1, drawing_rels[relation_id]))
    return result


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value.strip("._") or "image"


def read_images(path: Path, standard_name: str, standard_code: str, media_root: Path) -> list[dict]:
    workbook = load_workbook(path, read_only=False, data_only=True)
    try:
        if IMAGE_SHEET not in workbook.sheetnames:
            return []
        sheet = workbook[IMAGE_SHEET]
        captions = []
        for row in range(2, sheet.max_row + 1):
            caption = str(sheet.cell(row, 1).value or "").strip()
            match = FIGURE_REFERENCE_RE.search(caption)
            if match:
                captions.append((row, caption, normalize_asset_number(match.group(1)), sheet.cell(row, 2).value))
    finally:
        workbook.close()

    output_dir = media_root / standard_code
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_file in output_dir.glob("*"):
        if old_file.is_file():
            old_file.unlink()

    records = []
    with zipfile.ZipFile(path) as archive:
        for index, (anchor_row, media_part) in enumerate(_drawing_images(archive, IMAGE_SHEET), 1):
            candidates = [item for item in captions if item[0] <= anchor_row]
            if not candidates:
                continue
            _, caption, number, page = candidates[-1]
            suffix = Path(media_part).suffix.lower() or ".png"
            filename = _safe_filename(f"{number}_{index}{suffix}")
            output_path = output_dir / filename
            output_path.write_bytes(archive.read(media_part))
            records.append({
                "id": f"{standard_name}|{IMAGE_SHEET}|{media_part}",
                "准则": standard_name,
                "条款号": "",
                "编号": number,
                "对应编号": base_asset_number(number),
                "标题": caption,
                "图片地址": f"/static/standard_media/{standard_code}/{filename}",
                "PDF页码": int(page) if isinstance(page, (int, float)) else page,
                "来源文件": path.name,
                "工作表": IMAGE_SHEET,
                "源行": anchor_row,
            })
    return records


def attach_assets(standard_records: list[dict], tables: list[dict], images: list[dict]) -> tuple[int, int]:
    """按条款正文标注挂载附件；没有标注时用图/表基本编号作兜底。"""
    tables_by_number = defaultdict(list)
    images_by_number = defaultdict(list)
    for item in tables:
        tables_by_number[item["对应编号"]].append(item)
    for item in images:
        images_by_number[item["对应编号"]].append(item)

    table_links = image_links = 0
    for record in standard_records:
        clause_number = normalize_asset_number(record["条款号"])
        table_numbers = table_marker_numbers(record["条款内容"])
        figure_numbers = figure_marker_numbers(record["条款内容"])
        if not table_numbers:
            table_numbers = referenced_numbers(record["条款内容"], TABLE_REFERENCE_RE)
        if not figure_numbers:
            figure_numbers = referenced_numbers(record["条款内容"], FIGURE_REFERENCE_RE)
        selected_tables = []
        selected_images = []
        for number in table_numbers:
            selected_tables.extend(tables_by_number.get(base_asset_number(number), []))
        for number in figure_numbers:
            selected_images.extend(images_by_number.get(base_asset_number(number), []))
        if not selected_tables:
            selected_tables.extend(tables_by_number.get(clause_number, []))
        if not selected_images:
            selected_images.extend(images_by_number.get(clause_number, []))

        record["表格"] = list({item["id"]: item for item in selected_tables}.values())
        record["图片"] = list({item["id"]: item for item in selected_images}.values())
        for item in record["表格"]:
            item["条款号"] = record["条款号"]
        for item in record["图片"]:
            item["条款号"] = record["条款号"]
        table_links += len(record["表格"])
        image_links += len(record["图片"])
    return table_links, image_links


def read_standard_assets(paths: list[Path], standard_records: list[dict], media_root: Path) -> tuple[list[dict], list[dict], int, int]:
    by_standard = defaultdict(list)
    for record in standard_records:
        by_standard[record["准则"]].append(record)

    all_tables = []
    all_images = []
    for path in paths:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook["条款总表"]
            headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
            columns = {name: index for index, name in enumerate(headers) if name}
            standard_name = ""
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if columns.get("准则") is not None and row[columns["准则"]]:
                    standard_name = str(row[columns["准则"]]).strip()
                    break
        finally:
            workbook.close()
        if not standard_name or standard_name not in by_standard:
            continue
        code = next(record.get("准则代码") for record in by_standard[standard_name] if record.get("准则代码"))
        all_tables.extend(read_tables(path, standard_name))
        all_images.extend(read_images(path, standard_name, code, media_root))

    table_links = image_links = 0
    for standard_name, records in by_standard.items():
        current_tables = [item for item in all_tables if item["准则"] == standard_name]
        current_images = [item for item in all_images if item["准则"] == standard_name]
        linked_tables, linked_images = attach_assets(records, current_tables, current_images)
        table_links += linked_tables
        image_links += linked_images
    return all_tables, all_images, table_links, image_links


def asset_note(record: dict) -> str:
    parts = []
    page = record.get("PDF页码")
    if page not in (None, ""):
        parts.append(f"PDF第{page}页")
    if record.get("表格"):
        names = "、".join(dict.fromkeys(item["标题"] for item in record["表格"]))
        parts.append(f"含表格：{names}")
    if record.get("图片"):
        image_notes = "、".join(
            f"{item['标题']}（{item['图片地址']}）" for item in record["图片"]
        )
        parts.append(f"含图片：{image_notes}")
    return "；".join(parts)


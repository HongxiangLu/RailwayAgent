"""将验收指标及两套规范条文导入 Neo4j。

安装依赖：pip install -r requirements.txt
脚本会按自身位置定位项目根目录和 .env，不依赖运行时工作目录。
每次直接运行都会全量重建目标数据库中的节点和关系。
"""

from pathlib import Path
import os
import re
from dotenv import load_dotenv
from uuid import NAMESPACE_URL, uuid4, uuid5
import json

from openpyxl import load_workbook
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXCEL_PATH = PROJECT_ROOT / "data" / "知识图谱260924.xlsx"
DEFAULT_STANDARD_PATHS = [
    PROJECT_ROOT / "data" / "TB10752-2018高速铁路桥涵工程施工质量验收标准.xlsx",
    PROJECT_ROOT / "data" / "TB10424-2018铁路混凝土工程施工质量验收标准.xlsx",
]
STANDARD_SHEET = "条款总表"
STANDARD_COLUMNS = ["准则", "一级标题", "二级标题", "项目类别", "条款号", "条款内容", "PDF页码"]
STANDARD_BY_CODE = {
    "A": "高速铁路桥涵工程施工质量验收标准",
    "B": "铁路混凝土工程施工质量验收标准",
}
STANDARD_CODE_BY_NAME = {name: code for code, name in STANDARD_BY_CODE.items()}

# Excel 列名与 Neo4j 节点标签分开定义。数据库界面使用“工程项目类别”和
# “工程项目子类别”，不会创建名为“工程项目”的节点。
PROJECT_LEVELS = [
    ("单位工程", "单位工程"),
    ("分部工程类别", "工程项目类别"),
    ("分部工程子类别", "工程项目子类别"),
    ("分项工程", "分项工程"),
]
PROJECT_COLUMNS = [source_column for source_column, _ in PROJECT_LEVELS]
PROJECT_LABELS = [graph_label for _, graph_label in PROJECT_LEVELS]
DETAIL_COLUMNS = [f"施工质量验收标准{level}级细目" for level in "一二三四"]
PROPERTY_COLUMNS = ["检验项目类型", "规范条款", "是否可量化指标", "参数取值范围"]
STANDARD_LABELS = ["准则", "一级标题", "二级标题", "项目类别", "条款", "条款内容"]
LABELS = PROJECT_LABELS + ["施工质量验收标准细目", "指标"] + STANDARD_LABELS
BUSINESS_COLUMNS = PROJECT_COLUMNS + DETAIL_COLUMNS + ["指标"] + PROPERTY_COLUMNS
LEGACY_LABELS = {
    "工程", "工程项目", "分部工程类别", "分部工程子类别", "分部工程名称",
    "单项工程", "分部工程", "桥梁工程", "检验项目", "规范条款", "取值范围",
}


def text(value):
    return "" if value is None else str(value).replace("\r\n", "\n").strip()


def read_records(excel_path):
    workbook = load_workbook(excel_path)
    try:
        sheet = workbook["验收条款汇总"]
        columns = {cell.value: cell.column for cell in sheet[1] if cell.value}
        # 先确认列名，避免在读取失败时修改原表。
        missing = [name for name in BUSINESS_COLUMNS if name not in columns]
        if missing:
            raise ValueError(f"Excel缺少列：{'、'.join(missing)}")
        changed = "记录编号" not in columns
        if changed:
            columns["记录编号"] = max(columns.values()) + 1
            sheet.cell(1, columns["记录编号"], "记录编号")

        records, used_ids = [], set()
        for row_number in range(2, sheet.max_row + 1):
            record = {
                name: text(sheet.cell(row_number, columns[name]).value)
                for name in BUSINESS_COLUMNS
            }
            # 仅填充顶层名称的空行不属于指标记录。
            if not any(record[name] for name in BUSINESS_COLUMNS if name != PROJECT_COLUMNS[0]):
                continue
            if not record[PROJECT_COLUMNS[0]]:
                raise ValueError(f"第{row_number}行未填写单位工程")
            cell = sheet.cell(row_number, columns["记录编号"])
            record_id = text(cell.value)
            if not record_id:
                record_id = f"ZB-{uuid4().hex}"
                cell.value = record_id
                changed = True
            if record_id in used_ids:
                raise ValueError(f"第{row_number}行记录编号重复：{record_id}")
            used_ids.add(record_id)
            record["id"] = record_id
            records.append(record)

        # 编号持久化成功后才能开始数据库写入；后续运行复用该编号。
        if changed:
            workbook.save(excel_path)
        return records
    finally:
        workbook.close()


def normalize_clause_number(value):
    """统一条款号格式，同时避免 Excel 数字出现 .0 后缀。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return text(value).replace(" ", "").replace("第", "").replace("条", "")


def normalize_pdf_page(value):
    if value in (None, ""):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_standard_records(paths):
    """读取规范条款；空层级沿用上一条有效值，不猜测缺失 PDF 页码。"""
    records = []
    seen = {}
    for path in paths:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            if STANDARD_SHEET not in workbook.sheetnames:
                raise ValueError(f"{path.name} 缺少工作表：{STANDARD_SHEET}")
            sheet = workbook[STANDARD_SHEET]
            headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
            columns = {name: index for index, name in enumerate(headers) if name}
            missing = [name for name in STANDARD_COLUMNS if name not in columns]
            if missing:
                raise ValueError(f"{path.name} 缺少列：{'、'.join(missing)}")
            hierarchy_names = STANDARD_COLUMNS[:4]
            inherited = {name: "" for name in hierarchy_names}
            for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
                if not any(value not in (None, "") for value in row):
                    continue
                record = {
                    name: row[index] if index < len(row) else None
                    for name, index in columns.items() if name in STANDARD_COLUMNS
                }
                for level, name in enumerate(hierarchy_names):
                    current = text(record.get(name))
                    if current:
                        if current != inherited[name]:
                            for child_name in hierarchy_names[level + 1:]:
                                inherited[child_name] = ""
                        inherited[name] = current
                    else:
                        record[name] = inherited[name]
                record = {name: text(record.get(name)) for name in STANDARD_COLUMNS}
                record["条款号"] = normalize_clause_number(record["条款号"])
                record["PDF页码"] = normalize_pdf_page(
                    row[columns["PDF页码"]] if columns["PDF页码"] < len(row) else None
                )
                # 源表中该行正文和相邻编号表明它实际是 1.0.10。
                if (
                    record["准则"] == STANDARD_BY_CODE["A"]
                    and record["条款号"] == "1.0.1"
                    and "配套使用" in record["条款内容"]
                ):
                    record["条款号"] = "1.0.10"
                required = ["准则", "一级标题", "条款号", "条款内容"]
                missing_values = [name for name in required if not record[name]]
                if missing_values:
                    raise ValueError(
                        f"{path.name} 第{row_number}行缺少：{'、'.join(missing_values)}"
                    )
                if record["准则"] not in STANDARD_CODE_BY_NAME:
                    raise ValueError(f"{path.name} 第{row_number}行准则名称无法识别：{record['准则']}")
                key = (record["准则"], record["条款号"])
                if key in seen:
                    raise ValueError(
                        f"规范条款号重复：{record['准则']} {record['条款号']}，"
                        f"位于第{seen[key]}行和第{row_number}行"
                    )
                seen[key] = row_number
                record["来源文件"] = path.name
                records.append(record)
        finally:
            workbook.close()
    return records


def expand_clause_range(start, end):
    """展开末级连续条款号，如 5.2.1～5.2.5。"""
    start_parts = start.split(".")
    end_parts = end.split(".")
    if (
        len(start_parts) == len(end_parts)
        and len(start_parts) >= 2
        and start_parts[:-1] == end_parts[:-1]
        and start_parts[-1].isdigit()
        and end_parts[-1].isdigit()
    ):
        first, last = int(start_parts[-1]), int(end_parts[-1])
        if first <= last and last - first <= 100:
            prefix = ".".join(start_parts[:-1])
            return [f"{prefix}.{number}" for number in range(first, last + 1)]
    return [start, end]


def parse_clause_references(value):
    """解析指标中的 [A]/[B] 条款、并列条款及连续条款范围。"""
    source = text(value)
    if not source:
        return []
    # 同一范围两端重复写代码时先合并，便于展开中间条款。
    source = re.sub(
        r"条\s*[～~至]\s*\[([AB])\]\s*第?",
        lambda match: f"～[RANGE_{match.group(1)}]",
        source,
    )
    markers = list(re.finditer(r"\[([AB])\]", source))
    references = []
    for index, marker in enumerate(markers):
        code = marker.group(1)
        end = markers[index + 1].start() if index + 1 < len(markers) else len(source)
        segment = source[marker.end():end]
        segment = segment.replace(f"[RANGE_{code}]", "")
        matches = list(re.finditer(r"\d+(?:\.\d+){1,2}", segment))
        position = 0
        while position < len(matches):
            current = matches[position]
            number = current.group(0)
            if position + 1 < len(matches):
                following = matches[position + 1]
                separator = segment[current.end():following.start()]
                if re.search(r"[～~至]", separator):
                    for expanded in expand_clause_range(number, following.group(0)):
                        references.append((code, expanded))
                    position += 2
                    continue
            references.append((code, number))
            position += 1
    # 去重但保留书写顺序。
    return list(dict.fromkeys(references))


def hierarchy_id(label, parent_id, name, level=0):
    key = json.dumps([label, parent_id, level, name], ensure_ascii=False)
    return str(uuid5(NAMESPACE_URL, key))


def clause_id(standard_name, clause_number):
    return hierarchy_id("条款", standard_name, clause_number)


def write_standard_record(tx, record):
    parent = None
    levels = [
        ("准则", record["准则"]),
        ("一级标题", record["一级标题"]),
        ("二级标题", record["二级标题"]),
        ("项目类别", record["项目类别"]),
    ]
    for label, name in levels:
        if not name or name in {"—", "-", "/"}:
            continue
        node_id = hierarchy_id(label, parent[1] if parent else "", name)
        tx.run(
            f"MERGE (n:`{label}` {{id: $id}}) SET n.name = $name",
            id=node_id, name=name,
        ).consume()
        if parent:
            tx.run(
                f"MATCH (p:`{parent[0]}` {{id: $parent_id}}), "
                f"(n:`{label}` {{id: $id}}) MERGE (p)-[:包含]->(n)",
                parent_id=parent[1], id=node_id,
            ).consume()
        parent = (label, node_id)

    standard_code = STANDARD_CODE_BY_NAME[record["准则"]]
    current_clause_id = clause_id(record["准则"], record["条款号"])
    tx.run(
        "MERGE (n:条款 {id: $id}) SET n.name = $number, n.`条款号` = $number, "
        "n.`准则代码` = $standard_code",
        id=current_clause_id, number=record["条款号"], standard_code=standard_code,
    ).consume()
    if parent:
        tx.run(
            f"MATCH (p:`{parent[0]}` {{id: $parent_id}}), (n:条款 {{id: $id}}) "
            "MERGE (p)-[:包含]->(n)",
            parent_id=parent[1], id=current_clause_id,
        ).consume()

    content_id = hierarchy_id("条款内容", current_clause_id, record["条款内容"])
    page = record["PDF页码"]
    note = f"PDF第{page}页" if page not in (None, "") else ""
    tx.run(
        "MERGE (n:条款内容 {id: $id}) SET n.name = $content, n.`备注` = $note, "
        "n.`PDF页码` = $page, n.`来源文件` = $source_file",
        id=content_id, content=record["条款内容"], note=note,
        page=page, source_file=record["来源文件"],
    ).consume()
    tx.run(
        "MATCH (c:条款 {id: $clause_id}), (n:条款内容 {id: $content_id}) "
        "MERGE (c)-[:包含]->(n)",
        clause_id=current_clause_id, content_id=content_id,
    ).consume()


def write_record(tx, record):
    parent = None
    levels = [
        (graph_label, record[source_column], 0)
        for source_column, graph_label in PROJECT_LEVELS
    ]
    levels += [
        ("施工质量验收标准细目", record[column], level)
        for level, column in enumerate(DETAIL_COLUMNS, 1)
    ]
    for label, name, level in levels:
        if not name:
            continue  # 空层跳过；/、否等非空内容正常创建。
        node_id = hierarchy_id(label, parent[1] if parent else "", name, level)
        properties = {"name": name}
        if level:
            properties["层级"] = level
        tx.run(
            f"MERGE (n:`{label}` {{id: $id}}) SET n += $properties",
            id=node_id, properties=properties,
        ).consume()
        if parent:
            tx.run(
                f"MATCH (p:`{parent[0]}` {{id: $parent_id}}), "
                f"(n:`{label}` {{id: $id}}) MERGE (p)-[:包含]->(n)",
                parent_id=parent[1], id=node_id,
            ).consume()
        parent = (label, node_id)

    properties = {"name": record["指标"]}
    properties.update({name: record[name] for name in PROPERTY_COLUMNS})
    tx.run(
        "MERGE (n:指标 {id: $id}) SET n += $properties",
        id=record["id"], properties=properties,
    ).consume()
    # 原记录移动到新路径时，替换旧挂载关系，不改变指标ID。
    tx.run(
        "MATCH ()-[r:包含指标]->(n:指标 {id: $id}) DELETE r",
        id=record["id"],
    ).consume()
    if parent:
        tx.run(
            f"MATCH (p:`{parent[0]}` {{id: $parent_id}}), "
            "(n:指标 {id: $id}) MERGE (p)-[:包含指标]->(n)",
            parent_id=parent[1], id=record["id"],
        ).consume()
    for code, number in parse_clause_references(record["规范条款"]):
        standard_name = STANDARD_BY_CODE[code]
        tx.run(
            "MATCH (n:指标 {id: $indicator_id}), (c:条款 {id: $clause_id}) "
            "MERGE (n)-[:依据]->(c)",
            indicator_id=record["id"], clause_id=clause_id(standard_name, number),
        ).consume()


def ensure_name_indexes(session):
    """为实际按名称搜索的节点建立非唯一文本索引，可重复运行。"""
    for label in LABELS:
        session.run(
            f"CREATE TEXT INDEX IF NOT EXISTS FOR (n:`{label}`) ON (n.name)"
        ).consume()


def drop_legacy_schema(session):
    """删除旧导入模型遗留的约束和索引，避免旧标签继续出现在图谱界面。"""
    stale_labels = LEGACY_LABELS - set(LABELS)
    for constraint in list(session.run("SHOW CONSTRAINTS")):
        if set(constraint["labelsOrTypes"] or []) & stale_labels:
            name = constraint["name"].replace("`", "``")
            session.run(f"DROP CONSTRAINT `{name}` IF EXISTS").consume()
            print(f"已移除旧约束：{name}")
    for index in list(session.run("SHOW INDEXES")):
        if (
            index["owningConstraint"] is None
            and set(index["labelsOrTypes"] or []) & stale_labels
        ):
            name = index["name"].replace("`", "``")
            session.run(f"DROP INDEX `{name}` IF EXISTS").consume()
            print(f"已移除旧索引：{name}")


def import_graph(
    excel_path, uri, user, password, database="neo4j", clear_database=False,
    standard_paths=None,
):
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        records = read_records(excel_path)
        standard_records = read_standard_records(standard_paths or DEFAULT_STANDARD_PATHS)
        with driver.session(database=database) as session:
            if clear_database:
                session.run("MATCH (n) DETACH DELETE n").consume()
                drop_legacy_schema(session)
                # 清空数据不会移除约束；只移除原导入模型的name唯一约束。
                old_labels = set(LABELS)
                for constraint in list(session.run("SHOW CONSTRAINTS")):
                    if (
                        constraint["type"] in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"}
                        and constraint["entityType"] == "NODE"
                        and constraint["properties"] == ["name"]
                        and set(constraint["labelsOrTypes"]) & old_labels
                    ):
                        name = constraint["name"].replace("`", "``")
                        session.run(f"DROP CONSTRAINT `{name}`").consume()
                        print(f"已移除旧name唯一约束：{name}")
                print(f"已清空数据库：{database}")

            for label in LABELS:
                session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.id IS UNIQUE"
                ).consume()
            ensure_name_indexes(session)
            for count, record in enumerate(standard_records, 1):
                session.execute_write(write_standard_record, record)
                if count % 100 == 0:
                    print(f"已导入 {count}/{len(standard_records)} 条规范条款")
            print(f"规范条款导入完成：{len(standard_records)} 条")
            for count, record in enumerate(records, 1):
                session.execute_write(write_record, record)
                if count % 100 == 0:
                    print(f"已导入 {count}/{len(records)} 条指标记录")
            linked_indicators = session.run(
                "MATCH (n:指标)-[:依据]->(:条款) RETURN count(DISTINCT n) AS count"
            ).single()["count"]
            reference_links = session.run(
                "MATCH (:指标)-[r:依据]->(:条款) RETURN count(r) AS count"
            ).single()["count"]
        print(
            f"导入完成：{len(records)} 条指标、{len(standard_records)} 条规范条款；"
            f"{linked_indicators} 条指标建立 {reference_links} 条依据关系；数据库：{database}"
        )


def main():
    """读取项目配置并全量导入；供 PyCharm 右键 Run 直接调用。"""
    load_dotenv(PROJECT_ROOT / ".env")
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "")
    neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
    if not neo4j_password:
        raise ValueError("请在项目根目录 .env 中配置 NEO4J_PASSWORD")
    excel_setting = os.getenv("EXCEL_PATH", "").strip()
    excel_path = Path(excel_setting).expanduser() if excel_setting else DEFAULT_EXCEL_PATH
    if not excel_path.is_absolute():
        excel_path = PROJECT_ROOT / excel_path
    excel_path = excel_path.resolve()
    if not excel_path.is_file():
        raise FileNotFoundError(f"Excel 文件不存在：{excel_path}")
    missing_standard_files = [path for path in DEFAULT_STANDARD_PATHS if not path.is_file()]
    if missing_standard_files:
        raise FileNotFoundError(
            "规范文件不存在：" + "、".join(str(path) for path in missing_standard_files)
        )

    print("=" * 60)
    print("开始全量重建知识图谱")
    print(f"Excel：{excel_path}")
    for path in DEFAULT_STANDARD_PATHS:
        print(f"规范：{path}")
    print(f"Neo4j：{neo4j_uri} / {neo4j_database}")
    print("图谱层级：单位工程 → 工程项目类别 → 工程项目子类别 → 分项工程")
    print("规范层级：准则 → 一级标题 → 二级标题 → 项目类别 → 条款 → 条款内容")
    print("=" * 60, flush=True)
    try:
        import_graph(
            excel_path, neo4j_uri, neo4j_user, neo4j_password,
            neo4j_database, clear_database=True, standard_paths=DEFAULT_STANDARD_PATHS,
        )
    except ServiceUnavailable as exc:
        raise SystemExit(
            "导入失败：无法连接 Neo4j。请先启动 Neo4j 数据库；不需要启动 run.py。"
        ) from exc
    except AuthError as exc:
        raise SystemExit("导入失败：Neo4j 用户名或密码不正确，请检查 .env。") from exc


if __name__ == "__main__":
    main()

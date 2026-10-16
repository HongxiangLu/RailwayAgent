"""将知识图谱260924.xlsx导入Neo4j；首次运行会将记录编号保存回Excel。

安装依赖：pip install -r requirements.txt
在项目根目录运行：python -B app/import_graph.py
在项目根目录 .env 配置 EXCEL_PATH 和 NEO4J_* 后运行。CLEAR_DATABASE=True会清空指定数据库的全部节点和关系。
"""

from pathlib import Path
import os
import argparse
from dotenv import load_dotenv
from uuid import NAMESPACE_URL, uuid4, uuid5
import json

from openpyxl import load_workbook
from neo4j import GraphDatabase


PROJECT_COLUMNS = ["单项工程", "单位工程", "分部工程", "分项工程"]
DETAIL_COLUMNS = [f"施工质量验收标准{level}级细目" for level in "一二三四"]
PROPERTY_COLUMNS = ["检验项目类型", "规范条款", "是否可量化指标", "参数取值范围"]
LABELS = PROJECT_COLUMNS + ["施工质量验收标准细目", "指标"]
BUSINESS_COLUMNS = PROJECT_COLUMNS + DETAIL_COLUMNS + ["指标"] + PROPERTY_COLUMNS


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
                raise ValueError(f"第{row_number}行未填写单项工程")
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


def hierarchy_id(label, parent_id, name, level=0):
    key = json.dumps([label, parent_id, level, name], ensure_ascii=False)
    return str(uuid5(NAMESPACE_URL, key))


def write_record(tx, record):
    parent = None
    levels = [(label, record[label], 0) for label in PROJECT_COLUMNS]
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


def ensure_name_indexes(session):
    """为实际按名称搜索的六类节点建立非唯一文本索引，可重复运行。"""
    for label in LABELS:
        session.run(
            f"CREATE TEXT INDEX IF NOT EXISTS FOR (n:`{label}`) ON (n.name)"
        ).consume()


def import_graph(excel_path, uri, user, password, database="neo4j", clear_database=False):
    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        records = read_records(excel_path)
        with driver.session(database=database) as session:
            if clear_database:
                session.run("MATCH (n) DETACH DELETE n").consume()
                # 清空数据不会移除约束；只移除原导入模型的name唯一约束。
                old_labels = set(LABELS + ["工程", "检验项目", "规范条款"])
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
            for count, record in enumerate(records, 1):
                session.execute_write(write_record, record)
                if count % 100 == 0:
                    print(f"已导入 {count}/{len(records)} 条指标记录")
        print(f"导入完成：{len(records)} 条指标记录，数据库：{database}")


if __name__ == "__main__":
    # 拒绝未知参数，避免旧命令被忽略后意外进入全量导入。
    parser = argparse.ArgumentParser(description="从 Excel 导入知识图谱")
    parser.parse_args()
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(PROJECT_ROOT / ".env")
    NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
    NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
    NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
    if not NEO4J_PASSWORD:
        raise ValueError("请在项目根目录 .env 中配置 NEO4J_PASSWORD")
    excel_setting = os.getenv("EXCEL_PATH", "").strip()
    if not excel_setting:
        raise ValueError("请在项目根目录 .env 中配置 EXCEL_PATH")
    EXCEL_PATH = Path(excel_setting).expanduser()
    if not EXCEL_PATH.is_absolute():
        EXCEL_PATH = PROJECT_ROOT / EXCEL_PATH
    EXCEL_PATH = EXCEL_PATH.resolve()
    if not EXCEL_PATH.is_file():
        raise FileNotFoundError(f"Excel 文件不存在：{EXCEL_PATH}")

    CLEAR_DATABASE = True  # 全量重建时改为True；会清空此数据库全部节点和关系。

    import_graph(
        EXCEL_PATH, NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
        NEO4J_DATABASE, CLEAR_DATABASE,
    )

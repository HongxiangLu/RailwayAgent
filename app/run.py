import os
import json
import re
import sys
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from neo4j import GraphDatabase
from openai import OpenAI

# ============ 设置编码 ============
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# 配置路径固定在项目根目录，不依赖启动时所在目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

# ============ 初始化 Flask ============
app = Flask(__name__, template_folder="web")
CORS(app)

# ============ Neo4j 配置 ============
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# ============ 大模型配置 ============
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "deepseek-chat")

if not OPENAI_API_KEY:
    raise RuntimeError("请在项目根目录 .env 中配置 OPENAI_API_KEY，问答服务必须使用大模型。")
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)

# ============ 系统提示词 ============
SYSTEM_PROMPT = """
你是 Neo4j 查询专家。根据问题，只输出一条只读 Cypher，不输出解释。
以下结构严格对应 app/import_graph.py，不能假设其他标签或关系存在。

节点与属性：
- 层级标签：单项工程、单位工程、分部工程、分项工程，属性 id、name。
- 施工质量验收标准细目：属性 id、name、层级（整数1至4）。
- 指标：id 是记录编号，name 可以为空；另有字符串属性
  检验项目类型、规范条款、是否可量化指标、参数取值范围。
- 所有层级节点通过 包含 关系连接；完整顺序是四级工程再接一至四级细目。
  空层被跳过，不能要求每一层都存在。指标通过 包含指标 挂在最后一个非空层级下，
  可能直接挂在任意工程层级，或挂在细目下。
- 不存在 工程、检验类型、规范条款、取值范围 这些独立节点标签，
  不存在 依据、检验类型、取值范围 关系。
- 同名节点可能属于不同父级，指标按记录编号区分，不得仅按名称合并不同记录。

查询要求：
1. 模糊匹配 name；用户可能提到工程、细目或指标名称，分别在对应层级查找。
   不确定关键词属于哪一级时可在从单项工程出发的路径节点中匹配。
2. 使用有界可变长度 包含 路径兼容缺失层；完整层级最多8个节点，最多7条包含边。
   从分项工程到指标父节点使用 [:包含*0..4]，0跳覆盖直接挂载。
3. 条款、主控/一般项目、取值范围、可量化属性都读取指标属性。
4. 返回普通标量或列表/映射，不返回原始 path、node、relationship。
   指标明细保留工程/细目路径，避免混淆同名指标。id、记录编号及节点内部标识仅供系统使用，
   不得投影到 RETURN 或返回的映射中，也不得换别名返回。不要返回 properties(node) 或 node{.*}。
5. 只使用 MATCH、WHERE、WITH、RETURN、ORDER BY、LIMIT 等只读查询子句；
   不使用写入语句或过程调用。默认 LIMIT 30。不编造数据库中不存在的字段。
6. 对细目或指标的要求查询，应返回条款、检验项目类型、参数取值范围，空属性不能猜测。

示例：斜拉桥包含哪些分部工程？（关键词也可能位于单位工程层）
MATCH path=(root:单项工程)-[:包含*1..3]->(sp:分部工程)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "斜拉桥")
RETURN DISTINCT root.name AS 单项工程, sp.name AS 分部工程 LIMIT 30

示例：斜拉桥钢主梁涂装有什么要求？
MATCH path=(root:单项工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "斜拉桥")
  AND any(n IN nodes(path) WHERE n.name CONTAINS "钢主梁")
  AND any(n IN nodes(path) WHERE n.name CONTAINS "涂装")
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`检验项目类型` AS 检验项目类型,
       ind.`规范条款` AS 规范条款, ind.`参数取值范围` AS 参数取值范围 LIMIT 30

示例：钢桥面板依据什么规范条款？
MATCH path=(root:单项工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "钢桥面板")
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`规范条款` AS 规范条款 LIMIT 30

示例：涂装的主控项目有哪些？
MATCH path=(root:单项工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "涂装")
  AND ind.`检验项目类型` CONTAINS "主控"
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`规范条款` AS 规范条款,
       ind.`参数取值范围` AS 参数取值范围 LIMIT 30

示例：扭矩系数范围是多少？（指标名称也可能记录在细目中）
MATCH path=(root:单项工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "扭矩系数")
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`参数取值范围` AS 参数取值范围,
       ind.`是否可量化指标` AS 是否可量化指标 LIMIT 30
"""

# ============ 核心函数 ============

def generate_cypher(question: str) -> str:
    """调用大模型生成新图谱结构的 Cypher。"""
    try:
        question = question.encode('utf-8').decode('utf-8')
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"用户问题：{question}\n生成 Cypher："}
            ],
            temperature=0.1,
            max_tokens=1500
        )
        cypher = (response.choices[0].message.content or "").strip()
        cypher = re.sub(r'^```cypher\s*', '', cypher)
        cypher = re.sub(r'^```\s*', '', cypher)
        cypher = re.sub(r'\s*```$', '', cypher)
        if not cypher:
            raise ValueError("模型未返回查询语句")
        return cypher
    except Exception as e:
        raise RuntimeError("大模型生成查询失败，请检查模型配置、服务可用性及返回内容。") from e


def public_query_data(value):
    """移除误返回的内部标识，同时保留条款号、业务数值和记录顺序。"""
    if hasattr(value, 'items'):
        result = {}
        for key, item in value.items():
            normalized = str(key).replace('`', '').replace('_', '').lower()
            if normalized in {'id', '记录编号', '分部编号', '节点id', '内部id',
                              'recordid', 'nodeid', 'elementid'}:
                continue
            if normalized.endswith('.id') or normalized.startswith(('id(', 'elementid(')):
                continue
            # 导入脚本生成的记录ID/层级UUID即使被换别名，也不进入公开结果。
            if isinstance(item, str) and re.fullmatch(
                r'(?:ZB-[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})',
                item, re.IGNORECASE
            ):
                continue
            result[key] = public_query_data(item)
        return result
    if isinstance(value, list):
        return [public_query_data(item) for item in value]
    return value


def execute_query(cypher: str) -> list:
    """执行 Cypher 查询"""
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run(cypher)
            records = result.data()
            processed = []
            for record in records:
                item = {}
                for key, value in record.items():
                    if hasattr(value, 'items'):
                        item[key] = dict(value)
                    elif isinstance(value, list):
                        item[key] = [dict(v) if hasattr(v, 'items') else v for v in value]
                    else:
                        item[key] = value
                processed.append(item)
            return public_query_data(processed)
    except Exception as e:
        raise RuntimeError("图谱查询失败，请检查数据库连接及生成的 Cypher。") from e


def format_answer(question: str, data: list) -> str:
    """格式化答案"""
    data = public_query_data(data)
    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": "仅依据查询结果回答问题。保留工程和细目上下文、规范条款及数值单位。不要输出记录编号、节点ID或其他系统内部标识，不混合不同记录的取值范围。空属性表示未提供，不得补造。结果为空时明确说明未找到相关数据，不使用常识补充验收标准。多条结果用列表呈现。"},
                {"role": "user",
                 "content": f"用户问题：{question}\n查询结果：{json.dumps(data, ensure_ascii=False, indent=2)}\n回答："}
            ],
            temperature=0.3,
            max_tokens=1500
        )
        answer = (response.choices[0].message.content or "").strip()
        if not answer:
            raise ValueError("模型未返回回答")
        return answer
    except Exception as e:
        raise RuntimeError("大模型生成回答失败，请检查模型服务及返回内容。") from e


# ============ API 接口 ============

@app.route("/")
def index():
    """聊天界面"""
    return render_template("index.html")


@app.route("/api")
def api_index():
    return jsonify({
        "service": "高铁桥梁质量验收智能问答系统",
        "endpoints": {
            "POST /ask": "发送问题，返回答案",
            "GET /ask?question=你的问题": "GET 方式提问",
            "GET /health": "健康检查"
        }
    })


@app.route("/ask", methods=["POST"])
def ask_post():
    try:
        data = request.get_json()
        if not data or "question" not in data:
            return jsonify({"error": "请提供 question 参数"}), 400

        question = data["question"]
        limit = data.get("limit", 20)

        cypher = generate_cypher(question)
        if not cypher:
            return jsonify({"error": "生成查询语句失败"}), 500

        results = execute_query(cypher)
        if results:
            results = results[:limit]

        answer = format_answer(question, results)

        return jsonify({
            "success": True,
            "question": question,
            "answer": answer,
            "cypher": cypher,
            "data": results
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/ask", methods=["GET"])
def ask_get():
    question = request.args.get("question")
    if not question:
        return jsonify({"error": "请提供 question 参数"}), 400

    limit = request.args.get("limit", 20, type=int)

    try:
        cypher = generate_cypher(question)
        if not cypher:
            return jsonify({"error": "生成查询语句失败"}), 500

        results = execute_query(cypher)
        if results:
            results = results[:limit]

        answer = format_answer(question, results)

        return jsonify({
            "success": True,
            "question": question,
            "answer": answer,
            "cypher": cypher,
            "data": results
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health")
def health():
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run("RETURN 1")
        return jsonify({"status": "ok", "neo4j": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "neo4j": "disconnected", "error": str(e)})


# ============ 启动服务 ============
if __name__ == "__main__":
    print("=" * 50)
    print("🚀 高铁桥梁质量验收智能问答系统启动")
    print("📍 聊天界面: http://localhost:5000")
    print("📮 POST /ask  (JSON: {'question': '你的问题'})")
    print("📮 GET  /ask?question=你的问题")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
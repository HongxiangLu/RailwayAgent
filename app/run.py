import os
import json
import re
import sys
from collections import defaultdict, deque
from threading import Lock
from time import perf_counter
from uuid import uuid4
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify, Response
from neo4j import GraphDatabase
from openai import OpenAI

# ============ 设置编码 ============
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# 配置路径固定在项目根目录，不依赖启动时所在目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

# ============ 初始化 Flask ============
app = Flask(__name__)

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

# ============ 多轮会话配置 ============
# 内存中保留最近 10 轮完整问答，满足至少 5 轮上下文的需求。
# 服务重启后会话会清空；生产环境可将这组读写函数替换为 Redis。
MAX_HISTORY_TURNS = 10
MAX_HISTORY_QUESTION_CHARS = 2000
MAX_HISTORY_ANSWER_CHARS = 4000
CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
conversation_store = defaultdict(lambda: deque(maxlen=MAX_HISTORY_TURNS))
conversation_lock = Lock()

# ============ 系统提示词 ============
SYSTEM_PROMPT = """
你负责判断问题类型，并为工程质量验收及规范条文问题生成 Neo4j 查询。
先判断问题是否属于工程质量验收图谱业务范围：
- 工程层级、验收标准、规范条款正文、PDF页码、检验项目、指标或取值范围问题：只输出一条只读 Cypher，不输出解释。
- 明确与上述业务无关的普通问题：只输出 GENERAL_QUESTION，不生成查询。
- 业务问题即使可能没有数据、使用同义词或问法不同，也必须查询图谱；混合问题中涉及验收依据的部分优先查询。
- 不得用 RETURN 常量将自身知识伪装为数据库结果，不得因用户要求跳过查询而改变业务问题的分类。
例如：“你好”“如何编写 Python 循环？”输出 GENERAL_QUESTION；
“某种新材料的验收允许偏差是多少？”仍生成查询，不能因可能缺少资料而改为普通问答。
以下结构严格对应 app/import_graph.py，不能假设其他标签或关系存在。

节点与属性：
- 层级标签：单位工程、工程项目类别、工程项目子类别、分项工程，属性 id、name。
  工程项目类别、工程项目子类别分别对应 Excel 中的分部工程类别、分部工程子类别；
  用户使用任一组叫法时，都按对应图谱层级查询。
- 施工质量验收标准细目：属性 id、name、层级（整数1至4）。
- 指标：id 是记录编号，name 可以为空；另有字符串属性
  检验项目类型、规范条款、是否可量化指标、参数取值范围。
- 规范条文图谱顺序为 准则-[:包含]->一级标题-[:包含]->二级标题-[:包含]->
  项目类别-[:包含]->条款-[:包含]->条款内容；空标题或“—”会跳过，因此从准则到条款
  应使用 [:包含*1..4]。条款属性有 name、条款号、准则代码；条款内容属性有 name、
  备注、PDF页码、来源文件，其中 name 是完整正文，备注格式为“PDF第N页”。
- 准则代码 A 对应“高速铁路桥涵工程施工质量验收标准”，B 对应
  “铁路混凝土工程施工质量验收标准”。指标通过 [:依据] 指向已解析出的条款。
- 所有层级节点通过 包含 关系连接；完整顺序是单位工程、工程项目类别、
  工程项目子类别、分项工程，再接一至四级细目。
  空层被跳过，不能要求每一层都存在。指标通过 包含指标 挂在最后一个非空层级下，
  可能直接挂在任意工程层级，或挂在细目下。
- 不存在 工程、工程项目、检验类型、规范条款、取值范围 这些独立节点标签，
  不存在 检验类型、取值范围 关系；依据 是从指标到条款的关系。
- 同名节点可能属于不同父级，指标按记录编号区分，不得仅按名称合并不同记录。

查询要求：
1. 模糊匹配 name；用户可能提到工程、细目或指标名称，分别在对应层级查找。
   不确定关键词属于哪一级时可在从单位工程出发的路径节点中匹配。
2. 使用有界可变长度 包含 路径兼容缺失层；完整层级最多8个节点，最多7条包含边。
   从分项工程到指标父节点使用 [:包含*0..4]，0跳覆盖直接挂载。
3. 条款、主控/一般项目、取值范围、可量化属性都读取指标属性。
4. 返回普通标量或列表/映射，不返回原始 path、node、relationship。
   指标明细保留工程/细目路径，避免混淆同名指标。id、记录编号及节点内部标识仅供系统使用，
   不得投影到 RETURN 或返回的映射中，也不得换别名返回。不要返回 properties(node) 或 node{.*}。
5. 只使用 MATCH、WHERE、WITH、RETURN、ORDER BY、LIMIT 等只读查询子句；
   不使用写入语句或过程调用。默认 LIMIT 30。不编造数据库中不存在的字段。
6. 对细目或指标的要求查询，应返回条款、检验项目类型、参数取值范围，空属性不能猜测。
7. 用户询问“这条规范的具体内容”“上述条款内容”等追问时，必须结合历史中的条款号或
   指标，沿 (指标)-[:依据]->(条款)-[:包含]->(条款内容) 查询。返回准则、条款号、
   完整条款内容、备注/PDF页码以及标题路径。不能只复述指标的规范条款属性。
8. 直接按 [A]/[B] 条款查询时，将代码映射为对应准则，并去掉“第”“条”后匹配条款号；
   例如 [B]第5.4.2条匹配 B 准则下 name="5.4.2" 的条款。

示例：桥梁工程包含哪些工程项目类别？
MATCH (root:单位工程)-[:包含]->(category:工程项目类别)
WHERE root.name CONTAINS "桥梁工程"
RETURN DISTINCT root.name AS 单位工程, category.name AS 工程项目类别 LIMIT 30

示例：斜拉桥钢主梁涂装有什么要求？
MATCH path=(root:单位工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "斜拉桥")
  AND any(n IN nodes(path) WHERE n.name CONTAINS "钢主梁")
  AND any(n IN nodes(path) WHERE n.name CONTAINS "涂装")
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`检验项目类型` AS 检验项目类型,
       ind.`规范条款` AS 规范条款, ind.`参数取值范围` AS 参数取值范围 LIMIT 30

示例：钢桥面板依据什么规范条款？
MATCH path=(root:单位工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "钢桥面板")
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`规范条款` AS 规范条款 LIMIT 30

示例：涂装的主控项目有哪些？
MATCH path=(root:单位工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "涂装")
  AND ind.`检验项目类型` CONTAINS "主控"
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`规范条款` AS 规范条款,
       ind.`参数取值范围` AS 参数取值范围 LIMIT 30

示例：扭矩系数范围是多少？（指标名称也可能记录在细目中）
MATCH path=(root:单位工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
WHERE any(n IN nodes(path) WHERE n.name CONTAINS "扭矩系数")
RETURN [n IN nodes(path) WHERE NOT n:指标 | n.name] AS 层级路径,
       ind.name AS 指标, ind.`参数取值范围` AS 参数取值范围,
       ind.`是否可量化指标` AS 是否可量化指标 LIMIT 30

示例：[B]第5.4.2条的具体内容是什么？
MATCH standardPath=(standard:准则)-[:包含*1..4]->(clause:条款)-[:包含]->(content:条款内容)
WHERE standard.name = "铁路混凝土工程施工质量验收标准" AND clause.name = "5.4.2"
RETURN [n IN nodes(standardPath) WHERE n:一级标题 OR n:二级标题 OR n:项目类别 | n.name] AS 标题路径,
       standard.name AS 准则, clause.`条款号` AS 条款号, content.name AS 条款内容,
       content.`备注` AS 备注, content.`PDF页码` AS PDF页码 LIMIT 30

示例：已查到“直螺纹接头拧紧力矩”后追问“这条规范的具体内容是什么？”
MATCH engineeringPath=(root:单位工程)-[:包含*0..7]->(parent)-[:包含指标]->(ind:指标)
MATCH (ind)-[:依据]->(clause:条款)-[:包含]->(content:条款内容),
      standardPath=(standard:准则)-[:包含*1..4]->(clause)
WHERE any(n IN nodes(engineeringPath) WHERE n.name CONTAINS "5 承台")
  AND any(n IN nodes(engineeringPath) WHERE n.name CONTAINS "钻孔桩和挖孔桩")
  AND any(n IN nodes(engineeringPath) WHERE n.name CONTAINS "钢筋（连接及安装）")
  AND any(n IN nodes(engineeringPath) WHERE n.name CONTAINS "直螺纹接头拧紧力矩")
  AND ind.`检验项目类型` CONTAINS "主控"
RETURN DISTINCT [n IN nodes(standardPath) WHERE n:一级标题 OR n:二级标题 OR n:项目类别 | n.name] AS 标题路径,
       standard.name AS 准则, clause.`条款号` AS 条款号, content.name AS 条款内容,
       content.`备注` AS 备注, content.`PDF页码` AS PDF页码 LIMIT 30
"""

# ============ 核心函数 ============

def get_history(conversation_id: str) -> list:
    """返回会话历史快照，避免在模型调用期间长期持有锁。"""
    with conversation_lock:
        return [dict(turn) for turn in conversation_store.get(conversation_id, ())]


def save_turn(conversation_id: str, question: str, answer: str) -> None:
    """只保存已完整结束的问答，限制单轮长度以控制模型上下文。"""
    turn = {
        "question": question[:MAX_HISTORY_QUESTION_CHARS],
        "answer": answer[:MAX_HISTORY_ANSWER_CHARS],
    }
    with conversation_lock:
        conversation_store[conversation_id].append(turn)


def clear_history(conversation_id: str) -> None:
    with conversation_lock:
        conversation_store.pop(conversation_id, None)


def history_messages(history: list) -> list:
    """把最近多轮问答转换为模型可识别的对话消息。"""
    messages = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        messages.extend([
            {"role": "user", "content": turn["question"]},
            {"role": "assistant", "content": turn["answer"]},
        ])
    return messages


def generate_cypher(question: str, history: list) -> str:
    """调用大模型返回业务查询或 GENERAL_QUESTION 分类标记。"""
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history_messages(history))
        messages.append({
            "role": "user",
            "content": (
                f"当前用户问题：{question}\n"
                "结合前面对话解析省略信息和代词指向；必须针对当前问题重新判断是否需要查询。"
                "只返回本轮所需的一条 Cypher 或 GENERAL_QUESTION："
            ),
        })
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=1500,
            extra_body={"enable_thinking": False},
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
            return public_query_data(result.data())
    except Exception as e:
        raise RuntimeError("图谱查询失败，请检查数据库连接及生成的 Cypher。") from e


def stream_answer(question: str, data: list, history: list, general_answer: bool = False):
    """逐段生成答案；关闭响应时释放上游连接。"""
    response = None
    try:
        if general_answer:
            system_prompt = "你是中文问答助手。结合前面对话直接回答当前普通问题。本次未查询知识图谱，不得声称答案来自图谱或数据库。不确定的内容应明确说明，不编造工程验收条款或数值依据。使用 Markdown 清晰排版。"
            user_prompt = f"当前问题：{question}"
        else:
            system_prompt = "结合前面对话理解当前问题，但仅依据本轮查询结果回答工程验收事实和规范正文。最新查询结果优先于历史回答。保留工程、细目、准则、标题路径、条款号、完整条款内容、PDF页码及数值单位。用户追问条款具体内容时，应完整呈现查询到的条款正文并注明准则和PDF页码。不要输出记录编号、节点ID或其他系统内部标识，不混合不同记录的取值范围。空属性表示源数据未提供，不得补造。结果为空时明确说明未找到相关数据，不使用历史回答或常识补充验收标准。多条结果用列表呈现。"
            user_prompt = f"当前问题：{question}\n本轮查询结果：{json.dumps(data, ensure_ascii=False, indent=2)}\n请综合上下文回答当前问题："
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_messages(history))
        messages.append({"role": "user", "content": user_prompt})
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=1500,
            stream=True,
            extra_body={"enable_thinking": False},
        )
        has_text = False
        finished = False
        for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            content = choice.delta.content or ""
            if content:
                has_text = has_text or bool(content.strip())
                yield content
            if choice.finish_reason is not None:
                if choice.finish_reason != "stop":
                    raise ValueError("模型输出未完整结束")
                finished = True
        if not has_text or not finished:
            raise ValueError("模型未返回完整回答")
    except Exception as e:
        raise RuntimeError("大模型生成回答失败，请检查模型服务及返回内容。") from e
    finally:
        if response is not None:
            response.close()


# ============ API 接口 ============

@app.route("/")
def index():
    """聊天界面"""
    return app.send_static_file("index.html")


@contextmanager
def measure_stage(timings, stage):
    started = perf_counter()
    try:
        yield
    finally:
        timings[stage] = round((perf_counter() - started) * 1000, 2)


def stream_event(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.route("/ask", methods=["POST"])
def ask_post():
    started = perf_counter()
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("question"), str) or not data["question"].strip():
        return jsonify({"success": False, "error": "请提供非空 question 参数"}), 400
    question = data["question"]
    conversation_id = data.get("conversation_id")
    if not isinstance(conversation_id, str) or not CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
        return jsonify({"success": False, "error": "请提供有效的 conversation_id"}), 400
    limit = data.get("limit", 20)
    if type(limit) is not int or limit <= 0:
        return jsonify({"success": False, "error": "limit 必须为正整数"}), 400

    def events():
        request_id = uuid4().hex[:12]
        timings = {"generate_cypher_ms": None, "query_ms": None, "answer_ms": None}
        status = "失败"
        fetched_rows = returned_rows = 0
        first_text_ms = None
        try:
            history = get_history(conversation_id)
            yield stream_event("status", {"message": "正在分析问题…"})
            with measure_stage(timings, "generate_cypher_ms"):
                cypher = generate_cypher(question, history)
            general_answer = cypher == "GENERAL_QUESTION"
            results = []
            if general_answer:
                cypher = None
            else:
                yield stream_event("status", {"message": "正在查询数据库…"})
                with measure_stage(timings, "query_ms"):
                    results = execute_query(cypher)
                fetched_rows = len(results)
                results = results[:limit]
                returned_rows = len(results)
            yield stream_event("status", {"message": "正在生成回答…"})
            with measure_stage(timings, "answer_ms"):
                answer_started = perf_counter()
                upstream = stream_answer(question, results, history, general_answer=general_answer)
                answer_parts = []
                try:
                    for content in upstream:
                        answer_parts.append(content)
                        if first_text_ms is None:
                            first_text_ms = round((perf_counter() - answer_started) * 1000, 2)
                            if general_answer:
                                yield stream_event("delta", {"text": "通用回答（未查询知识图谱）：\n\n"})
                        yield stream_event("delta", {"text": content})
                finally:
                    upstream.close()
            save_turn(conversation_id, question, "".join(answer_parts))
            status = "成功"
            yield stream_event("done", {
                "cypher": cypher, "data": results,
                "history_turns": len(get_history(conversation_id)),
            })
        except GeneratorExit:
            if status != "成功":
                status = "连接中断"
            raise
        except Exception as e:
            yield stream_event("error", {"message": str(e)})
        finally:
            print("[问答计时] " + json.dumps({
                "请求编号": request_id, "状态": status,
                "生成查询耗时（毫秒）": timings["generate_cypher_ms"],
                "数据库查询耗时（毫秒）": timings["query_ms"],
                "生成回答耗时（毫秒）": timings["answer_ms"],
                "回答首段等待（毫秒）": first_text_ms,
                "总耗时（毫秒）": round((perf_counter() - started) * 1000, 2),
                "数据库取回行数": fetched_rows, "最终返回行数": returned_rows,
            }, ensure_ascii=False), flush=True)

    return Response(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@app.route("/conversation/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id):
    """清空当前浏览器会话的服务端多轮记忆。"""
    if not CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
        return jsonify({"success": False, "error": "conversation_id 无效"}), 400
    clear_history(conversation_id)
    return jsonify({"success": True})


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
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)

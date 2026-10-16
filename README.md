# 桥梁知识图谱问答系统（Flask）

入口：`app/run.py`；聊天模板：`app/web/index.html`；导入脚本：`app/import_graph.py`。

## 安装与配置

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 .\app\run.py
```

浏览器访问 http://localhost:5000 。

配置位于项目根目录 `.env`，参考 `.env.example`；已有配置不要覆盖。
Flask 和导入脚本共用根目录配置，按脚本位置定位，已有进程环境变量优先。

- `NEO4J_URI`、`NEO4J_USERNAME`、`NEO4J_PASSWORD`：数据库连接。
- `NEO4J_DATABASE`：数据库名，默认 `neo4j`，须与导入目标一致。
- `OPENAI_API_KEY`：必填，缺少时启动报错。
- `OPENAI_BASE_URL`、`OPENAI_MODEL`：模型地址和名称。

问答仅使用大模型：模型生成 Cypher → Neo4j 查询 → 模型根据结果回答。
模型调用或查询失败返回 `success: false`；不降级为规则问答。
空查询结果也交给模型说明未找到数据，不允许补造验收标准。

## 图谱结构

导入模型依次包含：单项工程、单位工程、分部工程、分项工程、最多四级施工质量验收标准细目。
空层跳过，层级之间使用 `包含`，指标通过 `包含指标` 挂到最后一个非空层级。
指标属性包括 `id`（记录编号）、`name`、`检验项目类型`、`规范条款`、`是否可量化指标`、`参数取值范围`。
条款和取值范围不是独立节点。记录编号仅用于图谱内部维护，不进入查询返回数据和问答；模型通过工程及细目层级路径区分同名指标。

## 数据导入注意事项

导入脚本从根目录 `.env` 读取 `EXCEL_PATH` 和共用的四个 Neo4j 配置项：
`NEO4J_URI`、`NEO4J_USERNAME`、`NEO4J_PASSWORD`、`NEO4J_DATABASE`。
`EXCEL_PATH=data/知识图谱260924.xlsx` 相对项目根目录解析，也支持绝对路径，与启动目录无关。
运行前会检查文件存在及密码已配置。

```powershell
.\.venv\Scripts\python.exe -X utf8 .\app\import_graph.py
```

当前脚本仍保留 `CLEAR_DATABASE=True`，运行会清空目标数据库，并可能回写 Excel 记录编号。
执行前核对目标库；只需增量导入时将脚本中的 `CLEAR_DATABASE` 改为 `False`。

导入时自动为六类节点的 `name` 创建非唯一 TEXT 索引，保留 `id` 唯一约束；索引进入 `ONLINE` 状态后可供查询使用。

## 接口

| 接口 | 功能 |
| --- | --- |
| GET / | 聊天页面 |
| GET /health | Neo4j 连通性检查（不验证模型） |
| POST /ask | JSON：`{"question":"斜拉桥包含哪些分部工程？","limit":20}` |

每次问答在终端输出 `[问答计时]` 日志，记录生成查询、数据库查询、生成回答及总处理耗时（毫秒），以及取回和返回行数；未执行阶段为 `null`，不记录问题、答案或凭据。总耗时不含浏览器与网络传输。

没有 `/docs` 和 `/stats`。当前使用 Flask 开发服务器并开启 debug，供本地调试。
模型输出的查询尚无完整语句级权限校验；提示词中的只读要求不能替代数据库只读权限。

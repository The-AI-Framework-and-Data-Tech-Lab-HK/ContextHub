# Deep Agents + ContextHub 入门教程

本教程会带你将一个 Deep Agent 接入 ContextHub，保存和检索一条记忆，将它共享给另一个智能体（agent），再按章节读取一份文档。

**开始前准备**

- 使用 macOS、Linux 或 WSL 中的 Bash / Zsh 终端。以下命令不是 PowerShell 语法。
- 运行完整本地演示需要 Git、curl、Python 3.12+ 和 Docker。开始第 2 步前，先启动 Docker Desktop 或 Docker 后台服务。
- 准备支持工具调用和文本向量化的模型服务。调用模型会产生相应服务商的用量费用。
- 第 1–3 步在同一个终端（**终端 1**）执行。启动服务后保持它运行，再用**终端 2**执行第 4–6 步和验证命令。
- 在第 1 步克隆仓库后，所有命令都在仓库根目录执行。环境变量不会自动在终端之间共享，请分别按教程配置。

**连接方式与职责**

```text
Deep Agent → native async tools → ContextHub Python SDK → ContextHub HTTP server
                                                            ↓
                                                   PostgreSQL + pgvector
```

- **适配器**：向智能体提供 ContextHub 工具。
- **Deep Agents**：管理对话和自己的文件系统。
- **ContextHub**：负责上下文的持久化存储、权限、版本和共享。
- **工具列表**：[工具参考](../../plugins/deepagents/README.md#tools)列出了全部十个工具，包括 `contexthub_ls`、`contexthub_read`、`contexthub_search`、`contexthub_stat`、`contexthub_sections` 和 `contexthub_read_section`。这些工具与 Deep Agents 内置的文件系统工具相互独立。
- **适用范围**：自动回忆与记忆采集、SQL 执行、基准测试集成不在此适配器的范围内。

**如果已经在运行 ContextHub**

1. 使用第 1.2 节中**只安装 SDK 和适配器**的命令，然后跳过第 2–3 步。这条路径不需要 Docker 或本地服务端包。
2. 在第 4 步填入现有的服务地址、API 密钥、账户和智能体 ID。下文的 `acme`、`query-agent`、`analysis-agent` 和 `engineering` 都是新建演示数据库中的值。
3. 第 5 步共享记忆时，需要两个对同一团队有写入权限的现有智能体，并替换命令和提示词中的 `analysis-agent`、`engineering`。第 6 步上传文档的智能体还需要根团队（`path = ""`）的写入权限。适配器不会自动创建这些成员关系。
4. 使用已有服务时，跳过针对一次性演示服务的真实模型验证和清理命令。

## 1. 安装

### 1.1. 克隆仓库

如果已经有包含 `plugins/deepagents/` 的仓库，直接进入其根目录，无需再次克隆。

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
```

### 1.2. 创建环境并安装依赖

- **新建环境**：明确使用 Python 3.12 的解释器。如果安装的是更高版本，请把 `python3.12` 换成对应命令，例如 `python3.13`；系统的 `python3` 可能仍指向旧版本。
- **已有智能体环境**：如果使用 Python 3.11+ 连接已有服务，激活原环境，跳过创建虚拟环境的命令，只执行下面安装 SDK 和适配器的命令。

```bash
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python --version
```

下面两种依赖安装方式**选一种**执行：

- **完整本地演示**：安装服务端、SDK、适配器和模型集成包。

```bash
python -m pip install -e . -e ./sdk -e ./plugins/deepagents langchain-openai greenlet
```

- **只连接已有服务**：在智能体环境中安装 SDK 和适配器，无需安装本地服务端包。

```bash
python -m pip install -e ./sdk -e ./plugins/deepagents langchain-openai
```

**依赖说明**

- **Python 版本**：单独使用适配器需要 3.11+；运行服务端需要 3.12+。
- **已验证的依赖组合**：Deep Agents 0.7.32、LangChain 1.4.8 和 langchain-core 1.7.4。
- **支持的 Deep Agents 版本**：从 0.7.32 起的 0.7.x 版本。
- **其他模型服务商**：将 `langchain-openai` 替换为对应服务商的 LangChain 集成包。

## 2. 创建独立的演示数据库

### 2.1. 启动数据库并检查是否就绪

- **端口**：演示数据库使用 **55432**，与可能已经运行在 5432 端口的数据库分开。
- **凭证**：下面的用户名和密码仅用于本地演示。
- **仅首次执行**：第 2 步中的容器创建和角色配置属于一次性初始化。复用这个演示环境时，不要重复创建容器或角色。

先确认 Docker 引擎已启动；下面的命令应返回引擎版本号：

```bash
docker info --format '{{.ServerVersion}}'
```

创建数据库容器：

```bash
docker run --name contexthub-deepagents-demo \
  -e POSTGRES_USER=contexthub_admin \
  -e POSTGRES_PASSWORD=local-admin \
  -e POSTGRES_DB=contexthub_deepagents_demo \
  -p 127.0.0.1:55432:5432 -d pgvector/pgvector:pg16
```

再单独执行就绪检查：

```bash
docker exec contexthub-deepagents-demo \
  pg_isready -h 127.0.0.1 -U contexthub_admin -d contexthub_deepagents_demo
```

**预期结果**：`accepting connections`。如果返回 `no response` 或 `rejecting connections`，等几秒后，只重新执行 `docker exec … pg_isready` 这条检查命令，不要重复执行 `docker run`。

### 2.2. 执行数据库迁移

```bash
export DATABASE_URL=postgresql://contexthub_admin:local-admin@localhost:55432/contexthub_deepagents_demo
alembic upgrade head
```

**迁移会创建的内容**

- 账户：`acme`。
- 智能体：`query-agent` 和 `analysis-agent`。
- 团队权限：两个智能体都对 `engineering` 团队拥有写入权限。

### 2.3. 创建应用角色并授予文档访问权限

以下命令只需执行一次，用于创建应用角色并授予权限。第 3 步会配置服务端使用这个角色连接数据库。

```bash
docker exec -i contexthub-deepagents-demo \
  psql -v ON_ERROR_STOP=1 -U contexthub_admin -d contexthub_deepagents_demo <<'SQL'
CREATE ROLE contexthub_app LOGIN PASSWORD 'local-app' NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE contexthub_deepagents_demo TO contexthub_app;
GRANT USAGE ON SCHEMA public TO contexthub_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO contexthub_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO contexthub_app;
INSERT INTO team_memberships (agent_id, team_id, role, access, is_primary)
SELECT a.agent_id, t.id, 'member', 'read_write', FALSE
FROM teams t CROSS JOIN (VALUES ('query-agent'), ('analysis-agent')) AS a(agent_id)
WHERE t.account_id = 'acme' AND t.path = '';
SQL
```

**为什么需要这些权限**

- **应用角色**：使用独立的非超级用户角色，让演示实际经过数据库的行级权限控制。
- **根团队成员关系**：支持文档导入和后面的可选在线验证。目前文档存放在根团队的 `ctx://resources/` 下。

## 3. 配置并启动 ContextHub——终端 1

继续使用第 1–2 步的终端，并保持 `.venv` 已激活。下面会明确将数据库连接从执行迁移的管理员切换为应用角色。

### 3.1. 设置服务端环境变量

- **模型用途**：服务端使用文本向量模型（embedding model）进行检索，使用对话模型生成文档章节。两者都使用服务端的 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
- **演示配置**：下面的 DashScope 配置经过实际在线验证。请换成你自己的密钥，并确认你的账户可以使用这两个模型。
- **其他服务**：如果使用其他兼容 OpenAI API 的服务，请替换服务地址和两个模型名称。只有服务支持 `CHAT_ENABLE_THINKING` 对应的参数时才设置它，否则终端和 `.env` 中都不应设置。切换服务时，执行 `unset CHAT_ENABLE_THINKING`，并删除 `.env` 中对应的配置（如果有）。

```bash
export DATABASE_URL=postgresql://contexthub_app:local-app@localhost:55432/contexthub_deepagents_demo
export API_KEY=deepagents-local-demo
export OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
export OPENAI_API_KEY=your-provider-key
export CHAT_MODEL=deepseek-v4-pro-0813
export CHAT_ENABLE_THINKING=false
export EMBEDDING_MODEL=text-embedding-v4
export EMBEDDING_DIMENSIONS=1536
export DOC_STORE_ROOT=./data/docs/deepagents-demo
export PROPAGATION_ENABLED=false
export LIFECYCLE_ENABLED=false
```

### 3.2. 启动 HTTP 服务

- 确保演示数据库的 `DATABASE_URL` 仍指向 **55432** 端口。
- 如果 HTTP 端口 **8000** 已被占用，请修改下面命令中的端口，并同步修改第 4 步的 `CONTEXTHUB_URL`。

```bash
uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

**预期结果**：Uvicorn 输出 `Application startup complete`，并持续运行。保持这个终端打开，接下来切换到终端 2。如果启动失败，先在这里解决数据库或配置错误，再继续。

**配置说明**

- **环境变量加载**：服务端也会读取仓库中的 `.env`，但通过 `export` 设置的环境变量优先级更高。
- **向量维度**：保持 `EMBEDDING_DIMENSIONS=1536`，与数据库结构一致。这个变量不会要求模型服务改变输出维度；当前客户端会为较短的向量补零，并拒绝超过 1536 维的向量，因此更换向量模型时需要确认其默认输出维度。
- **文档章节**：输出长度限制和回退机制见[文档章节生成](document-section-generation.md)。
- **后台任务**：本演示关闭了变更传播和定期生命周期清理。
- **变更传播的已知限制**：现有后台任务读取依赖关系时，没有为数据库连接绑定租户，因此使用上面的非超级用户角色会报错：`unrecognized configuration parameter "app.account_id"`。修复后台任务的租户绑定属于另一项服务端工作。
- **可用操作**：保存、共享、检索和章节读取仍可正常使用。本教程不验证后台变更传播。

## 4. 配置 Deep Agent——终端 2

### 4.1. 打开另一个终端，配置 ContextHub 连接

- 打开终端 2，进入同一个仓库根目录，并保持终端 1 中的演示服务运行。
- 激活第 1 步使用的同一个 Python 环境。如果不是 `.venv`，请将下面的激活命令替换为你实际使用的环境命令。
- 下面的连接配置对应一次性演示服务；连接已有服务时，请替换为实际配置。

```bash
source .venv/bin/activate
export CONTEXTHUB_URL=http://localhost:8000
export CONTEXTHUB_API_KEY=deepagents-local-demo
export CONTEXTHUB_ACCOUNT_ID=acme
export CONTEXTHUB_AGENT_ID=query-agent
```

### 4.2. 配置智能体使用的模型

将模型服务密钥替换为你自己的密钥：

```bash
export OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
export OPENAI_API_KEY=your-provider-key
export DEEPAGENTS_MODEL=openai:deepseek-v4-pro-0813
```

### 4.3. 检查能否访问 ContextHub

```bash
curl --fail-with-body --silent --show-error "$CONTEXTHUB_URL/health"
# {"status":"ok"}
```

**预期结果**：`{"status":"ok"}`。这个接口不需要认证，也不会调用模型，因此不能用它验证 API 密钥或模型配置。

### 4.4. 检查带认证的数据库请求

列出当前智能体可见的记忆，无需调用模型：

```bash
curl --fail-with-body --silent --show-error \
  "$CONTEXTHUB_URL/api/v1/memories" \
  -H "X-API-Key: $CONTEXTHUB_API_KEY" \
  -H "X-Account-Id: $CONTEXTHUB_ACCOUNT_ID" \
  -H "X-Agent-Id: $CONTEXTHUB_AGENT_ID"
```

**预期结果**：新建演示数据库返回 `[]`；已有服务可能返回可见记忆的 JSON 列表。这一步检查 API 密钥认证、必填身份请求头和数据库访问。团队权限会在第 5 步验证，模型调用也从那里开始。如果这里出现 HTTP 错误，先解决后再继续。

**两组模型配置的区别**

| 进程 | 配置 | 用途 |
|---|---|---|
| ContextHub 服务端 | `CHAT_MODEL`、`EMBEDDING_MODEL`，以及服务端使用的模型服务密钥和地址 | 文档章节生成和检索 |
| Deep Agent | `--model provider:model`，以及智能体使用的模型服务密钥和地址 | 推理和选择工具 |

- **服务商**：两个进程可以使用不同的模型服务商。`CHAT_ENABLE_THINKING` 只配置 ContextHub 服务端，不会配置 Deep Agent 的模型。
- **模型前缀**：`openai:` 表示使用 LangChain 的 OpenAI 兼容客户端，也适用于 DashScope。
- **环境变量加载**：智能体示例读取环境变量，**不会**自动加载 `.env` 或 `model_providers.local.json`。
- **ContextHub 认证**：`CONTEXTHUB_API_KEY` 对应的是服务端的 `API_KEY`，不是模型服务商的密钥。

## 5. 保存、检索和共享记忆

### 5.1. 保存一条私有记忆

让第一个智能体保存一条演示用的虚构事实：

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "请在 ContextHub 中将以下内容原样保存为一条私有记忆：Cedar 项目回滚需要审批码 CEDAR-842。返回保存后的 ctx:// URI。"
```

**预期结果**：返回 `ctx://agent/query-agent/memories/` 下保存的记忆 URI。等命令成功结束后，再执行下一条命令。

### 5.2. 在新对话中检索这条记忆

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "在 ContextHub 中搜索 Cedar 项目的回滚审批码，读取对应来源，并在回答中附上来源 URI。"
```

**检查结果**

- 回答应包含 `CEDAR-842`，并引用 `ctx://agent/query-agent/memories/` 下的来源。
- 这次调用会开启一段新对话。如果它能根据已保存的记忆回答，就说明 ContextHub 的持久化存储生效了。

### 5.3. 将记忆共享给团队

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "找到我关于 Cedar 项目回滚的私有记忆，将它共享到 ContextHub 的 engineering 团队，并返回共享后的 URI。"
```

**预期结果**：返回 `ctx://team/engineering/` 下的共享 URI。

### 5.4. 让另一个智能体读取共享记忆

```bash
CONTEXTHUB_AGENT_ID=analysis-agent \
  python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "在 ContextHub 中搜索 engineering 团队的 Cedar 项目回滚审批码。读取共享的来源，并引用其 URI。"
```

**预期结果**：第二个智能体读取 `ctx://team/engineering/` 下的团队副本，而不是第一个智能体的私有记忆。

**身份与权限说明**

- **临时切换读取者**：`CONTEXTHUB_AGENT_ID=analysis-agent` 只对这一条命令生效。后面的命令（包括第 6 步上传文档）仍使用先前通过 `export` 设置的 `query-agent` 身份。
- **身份固定**：账户和智能体身份在创建 SDK 客户端时就已确定，模型无法通过工具参数修改它们。
- **应用认证**：服务端 API 密钥用于认证可信应用，由该应用通过请求头提供身份信息。它不是单独发给每个智能体的凭证。

## 6. 上传并读取文档

### 6.1. 通过 SDK 上传示例文档

- 完成第 4 步后，在终端 2 执行；每个演示数据库只需上传一次。中英文教程使用同一个文档 URI，如果已经按任意一版上传过，直接跳到第 6.2 节；再次上传到相同 URI 会返回 HTTP 409。
- 上传本地文件是应用负责的操作，不属于提供给模型的十个工具。

```bash
python - <<'PY'
import asyncio
import os
import tempfile
from pathlib import Path
from contexthub_sdk import ContextHubClient

async def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cedar-runbook.md"
        path.write_text(
            "# Cedar runbook\n\n"
            "## Preparation\nSnapshot the database before deployment.\n\n"
            "## Rollback\nUse approval code CEDAR-842. Allow 12 minutes.\n",
            encoding="utf-8",
        )
        async with ContextHubClient(
            url=os.environ["CONTEXTHUB_URL"],
            api_key=os.environ["CONTEXTHUB_API_KEY"],
            account_id=os.environ["CONTEXTHUB_ACCOUNT_ID"],
            agent_id=os.environ["CONTEXTHUB_AGENT_ID"],
            timeout=120,
        ) as client:
            result = await client.document.ingest(
                uri="ctx://resources/manuals/cedar-runbook",
                file_path=str(path),
                tags=["deepagents-demo"],
            )
            print(result.model_dump_json(indent=2))

asyncio.run(main())
PY
```

**预期结果**：输出 JSON，其中 `uri` 为 `ctx://resources/manuals/cedar-runbook`，包含 `context_id`，且 `section_count` 大于 0。导入会调用服务端配置的模型，可能需要一些时间；等输出结果后再继续。示例将请求超时设为 120 秒。如果超时，先查看服务端日志，并通过第 6.2 节确认文档是否已经可读，再决定是否重新上传。

### 6.2. 让智能体读取回滚章节

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "查看 ctx://resources/manuals/cedar-runbook，列出它的章节，并读取回滚章节。其中规定了什么审批码，需要多长时间？请引用来源。"
```

**检查结果**

- **回答内容**：应包含 `CEDAR-842` 和 12 分钟。
- **工具调用顺序**：`contexthub_stat` → `contexthub_sections` → `contexthub_read_section`。
- **章节标识**：读取章节时使用的是 `contexthub_stat` 返回的上下文 ID，不是 URI。

**存储位置**

- **`DOC_STORE_ROOT`**：保存原始文件和提取出的文本。
- **PostgreSQL**：保存章节元数据和其他上下文记录。

## 7. 将工具接入你团队的智能体

本节是可选的应用接入示例。只定义 `answer` 函数不会发起调用；需要在你的异步代码中 `await answer(...)`，并传入问题、模型，以及 ContextHub 的连接和身份参数。

接入已有智能体时，将 ContextHub 工具追加到原工具列表，将使用说明追加到原系统提示词，并沿用现有模型、中间件、后端、状态检查点组件（checkpointer）及其他配置。

**接入步骤**

1. 使用目标 ContextHub 账户和智能体身份创建 SDK 客户端。
2. 将 `create_contexthub_tools(client)` 返回的工具列表加入智能体。
3. 在系统提示词中加入 `CONTEXTHUB_INSTRUCTIONS`。
4. 使用 `ainvoke` 或 `astream` 调用智能体，并让客户端保持打开，直到执行或流式输出结束。

```python
from contexthub_deepagents import CONTEXTHUB_INSTRUCTIONS, create_contexthub_tools
from contexthub_sdk import ContextHubClient
from deepagents import create_deep_agent

async def answer(question, model, *, url, api_key, account_id, agent_id):
    async with ContextHubClient(
        url=url, api_key=api_key, account_id=account_id, agent_id=agent_id,
    ) as client:
        agent = create_deep_agent(
            model=model,
            tools=create_contexthub_tools(client),
            system_prompt="你是一名研究助理。\n\n" + CONTEXTHUB_INSTRUCTIONS,
        )
        return await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
```

**使用说明**

- **异步调用**：同步方法 `invoke` 不支持这些工具。
- **子智能体**：Deep Agents 的默认子智能体会继承相同的工具和 ContextHub 身份。如果自定义子智能体需要不同的身份，请为它们分别创建客户端和工具。
- **对话线程**：更换线程 ID 不会改变 ContextHub 身份。
- **参考资料**：全部十个工具及 ContextHub 技能版本的处理方式，见[适配器参考](../../plugins/deepagents/README.md)。

## 验证与排查问题

在终端 2 的仓库根目录运行以下命令，并保持第 1 步的环境已激活。

### 运行离线测试

- 适配器和 SDK 的离线测试不需要启动服务端，也不需要模型密钥。

```bash
python -m pip install -e './sdk[dev]' -e './plugins/deepagents[dev]'
python -m pytest plugins/deepagents/tests tests/test_sdk.py -q
```

### 可选：使用真实模型验证

- **验证范围**：全部十个工具、权限拒绝、流式输出和指定版本的技能读取。
- **运行对象**：仅使用**本教程可随时丢弃的演示服务**。

```bash
python plugins/deepagents/examples/live_check.py \
  --model "$DEEPAGENTS_MODEL" --team engineering --reader analysis-agent \
  --output /tmp/contexthub-deepagents-demo-check
```

**预期结果**：命令正常结束，且输出目录中的 `report.json` 包含 `"status": "passed"`。如果失败，查看终端错误和已保存的调用追踪；`"status": "failed"` 不代表验证通过。

**输出与影响**

- **模型用量**：脚本会调用模型并写入虚构的测试记录。
- **输出文件**：调用追踪和 `report.json` 会保存到指定输出目录。
- **保留数据**：测试记录会留在演示数据库中，方便检查。
- **其他测试套件**：仓库中的数据库集成测试，其测试准备逻辑会清空数据表。不要连接现有的研究数据库来运行它们。

### 排查常见问题

| 现象 | 检查方法 |
|---|---|
| Python 版本报错或找不到 `python3.12` | 完整本地演示需要使用实际安装的 Python 3.12+ 解释器创建虚拟环境；激活后用 `python --version` 确认 |
| 无法连接 Docker 后台服务 | 启动 Docker Desktop 或 Docker 后台服务，先确认 `docker info` 成功，再创建数据库 |
| 容器名或数据库角色已经存在 | 说明已执行过初始化。需要全新演示时，先按下文清理，再重做第 2–3 步；如果第 2.2–2.3 节已经成功完成，可复用原容器（已停止时执行 `docker start contexthub-deepagents-demo`），从第 3 步继续 |
| 无法连接 8000 端口 | 保持终端 1 运行，等待启动完成，并确认 `CONTEXTHUB_URL` 与实际 HTTP 端口一致 |
| 缺少环境变量 | 在运行智能体的终端中，通过 `export` 设置四个 `CONTEXTHUB_*` 变量 |
| ContextHub 返回 HTTP 401 | 确认 `CONTEXTHUB_API_KEY` 与正在运行的服务端的 `API_KEY` 一致 |
| 模型服务商认证失败或额度报错 | 检查对应进程使用的模型服务账户、密钥和模型；这与 ContextHub 认证是两回事 |
| 共享记忆或导入文档时返回 HTTP 403 | 检查智能体的团队成员关系；导入文档需要根团队的写入权限 |
| 搜索没有结果 | 检查身份、可见范围、已保存的内容，以及服务端的文本向量模型配置 |
| 上传文档返回 HTTP 409 | 文档 URI 已存在。直接到第 6.2 节读取，或同时修改上传代码和读取提示词，使用一个新 URI |
| 上传文档返回 HTTP 503 | 在终端 1 为服务端配置非空的模型服务密钥，再重启服务；只在终端 2 设置密钥不会配置服务端 |
| 模型返回的章节内容为空或无效 | 检查服务端日志和[章节配置](document-section-generation.md)；通过回退机制成功导入，不代表模型生成的章节准确 |

### 清理演示数据

1. 在终端 1 按 Ctrl-C，停止演示服务。
2. 在终端 2 的仓库根目录执行下面的命令，删除演示专用的数据库容器和文档目录。**这些命令会删除演示数据。** 使用已有服务时跳过这些命令；如果修改过 `DOC_STORE_ROOT`，请使用实际的演示文档目录。

```bash
docker rm -f -v contexthub-deepagents-demo
rm -rf ./data/docs/deepagents-demo
```

## 实测记录（2026-11-03）

- **环境**：新建 Python 3.13.9 环境，使用 Docker 中的 PostgreSQL 16 + pgvector，以及非超级用户的应用角色。真实模型调用使用 DashScope 的 `deepseek-v4-pro-0813` 和 `text-embedding-v4`。
- **教程结果**：中英文版分别在全新的演示数据库中通过了完整流程，包括记忆保存、检索和共享，第二个智能体读取，文档上传和章节读取，以及第 7 节函数的实际调用。
- **额外检查**：两次真实模型验证均通过全部 8 组检查，覆盖十个工具、流式输出、技能版本、认证，以及私有记忆、团队和账户隔离。新环境中的 70 个离线测试全部通过。
- **验证记录**：[实测汇总](deepagents-tutorial-validation-20261103/summary.json)，包含测试环境、覆盖范围、两版教程的结果和耗时。
- **清理结果**：验证后已停止两个测试服务，并删除各自的专用容器、数据库和临时文档目录。实测汇总中的时间均为香港时间（UTC+8）。

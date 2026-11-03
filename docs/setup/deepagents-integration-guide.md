# Deep Agents + ContextHub tutorial

This tutorial connects a Deep Agent to ContextHub, saves and retrieves a memory,
shares it with another agent, and reads a document by section. Run commands from
the repository root. The complete demo needs Python 3.12+, Docker, and a model
endpoint supporting tool calls and embeddings. Model calls incur provider usage.

The connection is direct:

```text
Deep Agent → native async tools → ContextHub Python SDK → ContextHub HTTP server
                                                            ↓
                                                   PostgreSQL + pgvector
```

No OpenClaw bridge or sidecar is required. The adapter exposes tools; Deep Agents
still manages conversations and its own filesystem. ContextHub handles persistent
context, permissions, versions, and sharing. Automatic recall/capture, SQL
execution, and benchmark integrations are outside this adapter's scope.

Already running ContextHub? Install the SDK and adapter from step 1, then go to
step 4 with your server URL, API key, account, and existing agent memberships.
The `acme` identities below belong to the fresh demo database.

## 1. Install

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . -e ./sdk -e ./plugins/deepagents langchain-openai greenlet
```

The standalone adapter requires Python 3.11+; the server requires 3.12+.
The tested adapter stack is Deep Agents 0.7.32, LangChain 1.4.8, and
langchain-core 1.7.4. The package accepts Deep Agents 0.7.x from 0.7.32 onward.
For another model provider, install its LangChain provider package instead of
`langchain-openai`.

## 2. Create an isolated demo database

This database uses port **55432**, separate from an existing server on 5432.
The passwords below are local demo credentials.

```bash
docker run --name contexthub-deepagents-demo \
  -e POSTGRES_USER=contexthub_admin \
  -e POSTGRES_PASSWORD=local-admin \
  -e POSTGRES_DB=contexthub_deepagents_demo \
  -p 127.0.0.1:55432:5432 -d pgvector/pgvector:pg16

docker exec contexthub-deepagents-demo pg_isready -U contexthub_admin
```

Wait until the last command reports `accepting connections`, then migrate:

```bash
export DATABASE_URL=postgresql://contexthub_admin:local-admin@localhost:55432/contexthub_deepagents_demo
alembic upgrade head
```

Use a separate, non-superuser role for the application so database row-level
security is exercised. Migrations seed account `acme` and two agents,
`query-agent` and `analysis-agent`, with write access to team `engineering`.
The extra root-team memberships below enable document ingestion and the optional
live check: documents currently live under the root team's `ctx://resources/`.

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

export DATABASE_URL=postgresql://contexthub_app:local-app@localhost:55432/contexthub_deepagents_demo
```

## 3. Configure and start ContextHub — terminal 1

The server needs an embedding model for search and a chat model for document
section generation. Both use its `OPENAI_BASE_URL` and `OPENAI_API_KEY`.
The following is the DashScope configuration used in live validation; replace the
key with your own and confirm these models are available to your account.

```bash
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

uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

For another OpenAI-compatible endpoint, replace the base URL and both model
names. Leave `CHAT_ENABLE_THINKING` unset unless the endpoint supports that
parameter. See [document section generation](document-section-generation.md)
for the output-budget setting and fallback behavior.

The server also reads the repository's `.env`; exported variables override it.
Keep the demo's database URL pointing to port 55432. Choose a different HTTP port
if 8000 is already occupied, and update `CONTEXTHUB_URL` below to match.

This demo disables background change propagation and periodic lifecycle sweeps.
The existing propagation worker reads dependencies without binding a tenant on
that database connection, which fails under the non-superuser role above with
`unrecognized configuration parameter "app.account_id"`. Fixing worker tenant
binding is separate server work. Saving, sharing, retrieval, and section reads
remain available; this tutorial does not validate background propagation.

## 4. Configure the Deep Agent — terminal 2

Open another terminal at the repository root:

```bash
source .venv/bin/activate
export CONTEXTHUB_URL=http://localhost:8000
export CONTEXTHUB_API_KEY=deepagents-local-demo
export CONTEXTHUB_ACCOUNT_ID=acme
export CONTEXTHUB_AGENT_ID=query-agent

export OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
export OPENAI_API_KEY=your-provider-key
export DEEPAGENTS_MODEL=openai:deepseek-v4-pro-0813

curl "$CONTEXTHUB_URL/health"
# {"status":"ok"}
```

These are two independent model configurations:

| Process | Settings | Used for |
|---|---|---|
| ContextHub server | `CHAT_MODEL`, `EMBEDDING_MODEL`, server provider key/base URL | Document sections and retrieval |
| Deep Agent | `--model provider:model`, agent provider key/base URL | Reasoning and choosing tools |

They may use different providers. `openai:` selects LangChain's OpenAI-compatible
client, including for DashScope. The example reads environment variables; it
does **not** automatically load `.env` or `model_providers.local.json`.
`CONTEXTHUB_API_KEY` is the server's `API_KEY`, not a model-provider credential.

## 5. Save, retrieve, and share a memory

Ask the first agent to save a synthetic fact:

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Save this exact private memory in ContextHub: For project Cedar, rollback requires approval code CEDAR-842. Return the saved ctx:// URI."

python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Search ContextHub for project Cedar's rollback approval code, read its source, and answer with the source URI."
```

The second invocation has a new conversation. An answer grounded in the stored
memory demonstrates persistence in ContextHub. Expect `CEDAR-842` and a source
under `ctx://agent/query-agent/memories/`.

Share the memory with the team:

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Find my private project Cedar rollback memory and promote it to the engineering team in ContextHub. Return the shared URI."

CONTEXTHUB_AGENT_ID=analysis-agent \
  python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Search ContextHub for the engineering team's project Cedar rollback approval code. Read the shared source and cite its URI."
```

The second agent should read the team copy under `ctx://team/engineering/`, not
the first agent's private memory. Account and agent identity are fixed when the
SDK client is constructed; the model cannot change them through tool arguments.
The server API key authenticates a trusted application that supplies these
identity headers; it is not a separate credential issued to each agent.

## 6. Upload and read a document

Upload through the SDK, then let the agent navigate the sections. Uploading local
files is an application operation, not one of the ten model-facing tools.
Run this once in the fresh demo database:

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

python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Inspect ctx://resources/manuals/cedar-runbook, list its sections, and read the rollback section. What code and duration does it specify? Cite the source."
```

Expect `CEDAR-842` and `12 minutes`. The tool sequence is `contexthub_stat` →
`contexthub_sections` → `contexthub_read_section`. Section reads use the context
ID returned by `contexthub_stat`, not the URI. Original files and extracted text are in
`DOC_STORE_ROOT`; section metadata and other context records are in PostgreSQL.

## 7. Add the tools to your team's agent

Keep your existing model, middleware, backend, and checkpointer. Create an SDK
client for the intended ContextHub identity and add its tool list:

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
            system_prompt="You are a research assistant.\n\n" + CONTEXTHUB_INSTRUCTIONS,
        )
        return await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
```

Use `ainvoke` or `astream`, keeping the client open until execution/streaming
finishes. Synchronous `invoke` does not support these tools. Default Deep Agents
subagents inherit the same tools and ContextHub identity; give custom subagents
separate clients/tools if they need distinct identities. Changing a conversation
thread ID does not change the ContextHub identity. See the
[adapter reference](../../plugins/deepagents/README.md) for all ten tools and
ContextHub skill-version behavior.

## Verify and troubleshoot

Offline adapter and SDK tests require no server or model key:

```bash
python -m pip install -e '.[dev]' -e './plugins/deepagents[dev]'
python -m pytest plugins/deepagents/tests tests/test_sdk.py -q
```

For a stricter real-model check of all ten tools, permission failures, streaming,
and versioned skill reads, use **only this disposable demo server**:

```bash
python plugins/deepagents/examples/live_check.py \
  --model "$DEEPAGENTS_MODEL" --team engineering --reader analysis-agent \
  --output /tmp/contexthub-deepagents-demo-check
```

The script makes model calls and writes synthetic records. It saves traces and
`report.json`; it leaves the records in the demo database for inspection. The
repository's database integration tests are a different suite whose fixtures
truncate tables; do not enable them against an existing research database.

| Symptom | Check |
|---|---|
| Missing environment variable | Export the four `CONTEXTHUB_*` settings in the agent terminal |
| ContextHub HTTP 401 | Match `CONTEXTHUB_API_KEY` to the running server's `API_KEY` |
| Model-provider authentication/quota error | Check that process's provider account, key, and model; this is separate from ContextHub authentication |
| Promotion or ingestion HTTP 403 | Check agent/team membership; root-team write access is needed for document ingestion |
| No search results | Check identity, visibility, saved content, and the server's embedding configuration |
| Empty/invalid model section output | Inspect server logs and [section configuration](document-section-generation.md); successful fallback ingestion does not establish model section accuracy |

When finished, stop the demo server with Ctrl-C. Remove its dedicated database
container and document directory (these commands delete the demo data):

```bash
docker rm -f -v contexthub-deepagents-demo
rm -rf ./data/docs/deepagents-demo
```

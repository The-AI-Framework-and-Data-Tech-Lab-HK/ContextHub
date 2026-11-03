# Deep Agents + ContextHub tutorial

This tutorial connects a Deep Agent to ContextHub, saves and retrieves a memory, shares it with another agent, and reads a document by section.

**Before you start**

- Use a Bash or Zsh terminal on macOS, Linux, or WSL. The commands below are not PowerShell syntax.
- Install Git, curl, Python 3.12+, and Docker for the complete local demo. Start Docker Desktop or the Docker daemon before step 2.
- Prepare a model endpoint supporting tool calls and embeddings. Model calls incur provider usage.
- Run steps 1–3 in the same terminal (**terminal 1**). After starting the server, leave it running and use **terminal 2** for steps 4–6 and the verification commands.
- Run all commands from the repository root after cloning it in step 1. Environment variables are local to each terminal; configure both terminals as shown.

**How the connection works**

```text
Deep Agent → native async tools → ContextHub Python SDK → ContextHub HTTP server
                                                            ↓
                                                   PostgreSQL + pgvector
```

- **Adapter:** exposes ContextHub tools to the agent.
- **Deep Agents:** manages conversations and its own filesystem.
- **ContextHub:** handles persistent context, permissions, versions, and sharing.
- **Tools:** the [tool reference](../../plugins/deepagents/README.md#tools) lists all ten tools, including `contexthub_ls`, `contexthub_read`, `contexthub_search`, `contexthub_stat`, `contexthub_sections`, and `contexthub_read_section`. These are separate from Deep Agents' built-in filesystem tools.
- **Scope:** automatic recall/capture, SQL execution, and benchmark integrations are outside this adapter's scope.

**If ContextHub is already running**

1. Use the **SDK/adapter-only** install in step 1.2, then skip steps 2–3. Docker and the local server package are unnecessary for this path.
2. In step 4, substitute your server URL, API key, account, and agent ID. The `acme`, `query-agent`, `analysis-agent`, and `engineering` values below belong to the fresh demo database.
3. For sharing in step 5, use two existing agents with write access to the same team; replace `analysis-agent` and `engineering` in the commands and prompts. For step 6, the uploading agent also needs write access to the root team (`path = ""`). The adapter does not create these memberships.
4. Skip the disposable-server live check and cleanup commands when using an existing service.

## 1. Install

### 1.1. Clone the repository

If you already have a checkout containing `plugins/deepagents/`, change to its root instead of cloning again.

```bash
git clone https://github.com/The-AI-Framework-and-Data-Tech-Lab-HK/ContextHub.git
cd ContextHub
```

### 1.2. Create an environment and install dependencies

- **New environment:** use the Python 3.12 executable explicitly. For a newer version, replace `python3.12` with that executable, such as `python3.13`; `python3` may still point to an older system Python.
- **Existing agent environment:** if connecting to an existing server from Python 3.11+, activate that environment and skip virtual-environment creation. Use only the SDK/adapter install below.

```bash
python3.12 --version
python3.12 -m venv .venv
source .venv/bin/activate
python --version
```

Choose **one** dependency installation:

- **Complete local demo:** install the server, SDK, adapter, and model integration.

```bash
python -m pip install -e . -e ./sdk -e ./plugins/deepagents langchain-openai greenlet
```

- **SDK/adapter only, for an existing server:** install into the agent environment; no local server package is needed.

```bash
python -m pip install -e ./sdk -e ./plugins/deepagents langchain-openai
```

**Dependency notes**

- **Python:** the standalone adapter requires 3.11+; the server requires 3.12+.
- **Tested stack:** Deep Agents 0.7.32, LangChain 1.4.8, and langchain-core 1.7.4.
- **Supported Deep Agents versions:** 0.7.x from 0.7.32 onward.
- **Other model providers:** install the corresponding LangChain provider package instead of `langchain-openai`.

## 2. Create an isolated demo database

### 2.1. Start the database and check readiness

- **Port:** the demo uses **55432**, separate from an existing server on 5432.
- **Credentials:** the usernames and passwords below are for local demo use.
- **First run only:** the container creation and role setup in step 2 are one-time initialization commands. If reusing this demo, do not recreate the container or role.

First confirm that the Docker engine is running; this should print its version:

```bash
docker info --format '{{.ServerVersion}}'
```

Create the database container:

```bash
docker run --name contexthub-deepagents-demo \
  -e POSTGRES_USER=contexthub_admin \
  -e POSTGRES_PASSWORD=local-admin \
  -e POSTGRES_DB=contexthub_deepagents_demo \
  -p 127.0.0.1:55432:5432 -d pgvector/pgvector:pg16
```

Check readiness with a separate command:

```bash
docker exec contexthub-deepagents-demo \
  pg_isready -h 127.0.0.1 -U contexthub_admin -d contexthub_deepagents_demo
```

**Expected result:** `accepting connections`. If it reports `no response` or `rejecting connections`, wait a few seconds and rerun only the `docker exec … pg_isready` command. Do not repeat `docker run`.

### 2.2. Run database migrations

```bash
export DATABASE_URL=postgresql://contexthub_admin:local-admin@localhost:55432/contexthub_deepagents_demo
alembic upgrade head
```

**What migrations create**

- Account: `acme`.
- Agents: `query-agent` and `analysis-agent`.
- Team access: both agents have write access to `engineering`.

### 2.3. Create the application role and grant document access

Run these commands once to create the application role and grant access. Step 3 will configure the server to connect with this role.

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

**Why these permissions are needed**

- **Application role:** a separate, non-superuser role ensures database row-level security is exercised.
- **Root-team memberships:** enable document ingestion and the optional live check. Documents currently live under the root team's `ctx://resources/`.

## 3. Configure and start ContextHub — terminal 1

Continue in the terminal used for steps 1–2, with `.venv` activated. The settings below explicitly switch from the migration administrator to the application database role.

### 3.1. Set the server environment variables

- **Models:** the server uses an embedding model for search and a chat model for document section generation. Both use the server's `OPENAI_BASE_URL` and `OPENAI_API_KEY`.
- **Demo configuration:** the DashScope settings below were used in live validation. Replace the key with your own and confirm both models are available to your account.
- **Other endpoints:** replace the base URL and both model names. Leave `CHAT_ENABLE_THINKING` unset in both the shell and `.env` unless the endpoint supports that parameter. When switching away from the demo configuration, run `unset CHAT_ENABLE_THINKING` and remove any matching `.env` entry.

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

### 3.2. Start the HTTP server

- Keep the demo's `DATABASE_URL` pointing to port **55432**.
- If HTTP port **8000** is occupied, choose another port in the command below and update `CONTEXTHUB_URL` in step 4 to match.

```bash
uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

**Expected result:** Uvicorn reports `Application startup complete` and stays running. Leave this terminal open; continue in terminal 2. If startup fails, resolve the database/configuration error here before continuing.

**Configuration notes**

- **Environment loading:** the server also reads the repository's `.env`; exported variables override it.
- **Embedding dimensions:** keep `EMBEDDING_DIMENSIONS=1536` to match the database schema. This setting does not request a different output size from the provider. The current client pads shorter vectors and rejects vectors longer than 1536, so check the native output size when changing embedding models.
- **Document sections:** see [document section generation](document-section-generation.md) for the output-budget setting and fallback behavior.
- **Background tasks:** this demo disables change propagation and periodic lifecycle sweeps.
- **Known propagation limitation:** the existing worker reads dependencies without binding a tenant on that database connection. Under the non-superuser role above, this fails with `unrecognized configuration parameter "app.account_id"`. Fixing worker tenant binding is separate server work.
- **Available operations:** saving, sharing, retrieval, and section reads remain available. This tutorial does not validate background propagation.

## 4. Configure the Deep Agent — terminal 2

### 4.1. Open another terminal and configure the ContextHub connection

- Open terminal 2 and change to the same repository root. Keep the demo server running in terminal 1.
- Activate the same Python environment used in step 1. If it is not `.venv`, replace the activation command below with your environment's command.
- The connection values below match the disposable demo; replace them when using an existing service.

```bash
source .venv/bin/activate
export CONTEXTHUB_URL=http://localhost:8000
export CONTEXTHUB_API_KEY=deepagents-local-demo
export CONTEXTHUB_ACCOUNT_ID=acme
export CONTEXTHUB_AGENT_ID=query-agent
```

### 4.2. Configure the agent's model

Replace the provider key with your own:

```bash
export OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
export OPENAI_API_KEY=your-provider-key
export DEEPAGENTS_MODEL=openai:deepseek-v4-pro-0813
```

### 4.3. Check that ContextHub is reachable

```bash
curl --fail-with-body --silent --show-error "$CONTEXTHUB_URL/health"
# {"status":"ok"}
```

**Expected result:** `{"status":"ok"}`. This endpoint does not require authentication or call a model; it does not validate your API key or model settings.

### 4.4. Check an authenticated database request

List the current agent's visible memories without invoking a model:

```bash
curl --fail-with-body --silent --show-error \
  "$CONTEXTHUB_URL/api/v1/memories" \
  -H "X-API-Key: $CONTEXTHUB_API_KEY" \
  -H "X-Account-Id: $CONTEXTHUB_ACCOUNT_ID" \
  -H "X-Agent-Id: $CONTEXTHUB_AGENT_ID"
```

**Expected result:** `[]` for the fresh demo, or a JSON list of visible memories on an existing server. This checks API-key authentication, required identity headers, and database access. Team permissions are exercised in step 5; model calls begin there too. Resolve any HTTP error before continuing.

**How the two model configurations differ**

| Process | Settings | Used for |
|---|---|---|
| ContextHub server | `CHAT_MODEL`, `EMBEDDING_MODEL`, server provider key/base URL | Document sections and retrieval |
| Deep Agent | `--model provider:model`, agent provider key/base URL | Reasoning and choosing tools |

- **Providers:** the two processes may use different providers. `CHAT_ENABLE_THINKING` configures only the ContextHub server, not the Deep Agent model.
- **Model prefix:** `openai:` selects LangChain's OpenAI-compatible client, including for DashScope.
- **Environment loading:** the agent example reads environment variables; it does **not** automatically load `.env` or `model_providers.local.json`.
- **ContextHub authentication:** `CONTEXTHUB_API_KEY` is the server's `API_KEY`, not a model-provider credential.

## 5. Save, retrieve, and share a memory

### 5.1. Save a private memory

Ask the first agent to save a synthetic fact:

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Save this exact private memory in ContextHub: For project Cedar, rollback requires approval code CEDAR-842. Return the saved ctx:// URI."
```

**Expected result:** a saved memory URI under `ctx://agent/query-agent/memories/`. Wait for the command to finish successfully before starting the next one.

### 5.2. Retrieve it in a new conversation

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Search ContextHub for project Cedar's rollback approval code, read its source, and answer with the source URI."
```

**Check the result**

- The answer should contain `CEDAR-842` and cite a source under `ctx://agent/query-agent/memories/`.
- This invocation starts a new conversation. An answer grounded in the stored memory demonstrates persistence in ContextHub.

### 5.3. Share the memory with the team

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Find my private project Cedar rollback memory and promote it to the engineering team in ContextHub. Return the shared URI."
```

**Expected result:** a shared URI under `ctx://team/engineering/`.

### 5.4. Read the shared memory as another agent

```bash
CONTEXTHUB_AGENT_ID=analysis-agent \
  python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Search ContextHub for the engineering team's project Cedar rollback approval code. Read the shared source and cite its URI."
```

**Expected result:** the second agent reads the team copy under `ctx://team/engineering/`, not the first agent's private memory.

**Identity and permissions**

- **Temporary reader switch:** `CONTEXTHUB_AGENT_ID=analysis-agent` applies only to that one command. Later commands, including the upload in step 6, still use the exported `query-agent` identity.
- **Fixed identity:** account and agent identity are set when the SDK client is constructed. The model cannot change them through tool arguments.
- **Application authentication:** the server API key authenticates a trusted application that supplies the identity headers. It is not a separate credential issued to each agent.

## 6. Upload and read a document

### 6.1. Upload the sample document through the SDK

- Run this in terminal 2 after step 4, once per demo database. The English and Chinese tutorials use the same document URI. If you already uploaded it using either tutorial, skip to step 6.2; another upload to that URI returns HTTP 409.
- Uploading local files is an application operation, not one of the ten model-facing tools.

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

**Expected result:** a JSON object with `uri` equal to `ctx://resources/manuals/cedar-runbook`, a `context_id`, and a positive `section_count`. Ingestion calls the server's model and can take time; wait for this output before continuing. The sample allows a 120-second request timeout. If it times out, inspect the server logs and check whether the document is already available in step 6.2 before retrying the upload.

### 6.2. Ask the agent to read the rollback section

```bash
python plugins/deepagents/examples/query.py --model "$DEEPAGENTS_MODEL" \
  "Inspect ctx://resources/manuals/cedar-runbook, list its sections, and read the rollback section. What code and duration does it specify? Cite the source."
```

**Check the result**

- **Answer:** expect `CEDAR-842` and `12 minutes`.
- **Tool sequence:** `contexthub_stat` → `contexthub_sections` → `contexthub_read_section`.
- **Section identifier:** section reads use the context ID returned by `contexthub_stat`, not the URI.

**Storage locations**

- **`DOC_STORE_ROOT`:** original files and extracted text.
- **PostgreSQL:** section metadata and other context records.

## 7. Add the tools to your team's agent

This optional section is a minimal integration pattern for your application. Defining `answer` does not run it: your async caller must `await answer(...)` with a question, model, and ContextHub connection/identity arguments.

For an existing agent, append the ContextHub tools to its current tool list and append the instructions to its existing system prompt. Keep its model, middleware, backend, checkpointer, and other configuration.

**Integration steps**

1. Create an SDK client for the intended ContextHub account and agent identity.
2. Add the tool list returned by `create_contexthub_tools(client)` to the agent.
3. Include `CONTEXTHUB_INSTRUCTIONS` in the system prompt.
4. Call the agent with `ainvoke` or `astream`, keeping the client open until execution or streaming finishes.

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

**Usage notes**

- **Async execution:** synchronous `invoke` does not support these tools.
- **Subagents:** default Deep Agents subagents inherit the same tools and ContextHub identity. Give custom subagents separate clients/tools if they need distinct identities.
- **Conversation threads:** changing the thread ID does not change the ContextHub identity.
- **Reference:** see the [adapter reference](../../plugins/deepagents/README.md) for all ten tools and ContextHub skill-version behavior.

## Verify and troubleshoot

Run these commands in terminal 2 from the repository root, with the environment from step 1 activated.

### Run the offline tests

- These adapter and SDK tests require no server or model key.

```bash
python -m pip install -e './sdk[dev]' -e './plugins/deepagents[dev]'
python -m pytest plugins/deepagents/tests tests/test_sdk.py -q
```

### Optional: run the real-model check

- **Coverage:** all ten tools, permission failures, streaming, and versioned skill reads.
- **Target:** use **only this disposable demo server**.

```bash
python plugins/deepagents/examples/live_check.py \
  --model "$DEEPAGENTS_MODEL" --team engineering --reader analysis-agent \
  --output /tmp/contexthub-deepagents-demo-check
```

**Expected result:** the command exits successfully and the output directory's `report.json` contains `"status": "passed"`. If it fails, inspect the terminal error and saved traces; a report with `"status": "failed"` is not a completed verification.

**Outputs and side effects**

- **Model usage:** the script makes model calls and writes synthetic records.
- **Saved output:** traces and `report.json` are written to the output directory.
- **Retained data:** the records remain in the demo database for inspection.
- **Other test suites:** the repository's database integration tests use fixtures that truncate tables. Do not enable them against an existing research database.

### Troubleshoot common issues

| Symptom | Check |
|---|---|
| Python version error or missing `python3.12` | Use your installed Python 3.12+ executable for the local demo and recreate the virtual environment with it; check `python --version` after activation |
| Docker daemon unavailable | Start Docker Desktop or the Docker daemon; `docker info` must succeed before creating the database |
| Container name or database role already exists | Initialization has already run. For an intentional fresh start, use the demo cleanup below and repeat steps 2–3; if steps 2.2–2.3 already completed successfully, reuse that container (`docker start contexthub-deepagents-demo` if stopped) and continue at step 3 |
| Connection refused at port 8000 | Keep terminal 1 running; wait for startup to finish and match `CONTEXTHUB_URL` to its HTTP port |
| Missing environment variable | Export the four `CONTEXTHUB_*` settings in the agent terminal |
| ContextHub HTTP 401 | Match `CONTEXTHUB_API_KEY` to the running server's `API_KEY` |
| Model-provider authentication/quota error | Check that process's provider account, key, and model; this is separate from ContextHub authentication |
| Promotion or ingestion HTTP 403 | Check agent/team membership; root-team write access is needed for document ingestion |
| No search results | Check identity, visibility, saved content, and the server's embedding configuration |
| Document upload HTTP 409 | The document URI already exists. Reuse it at step 6.2, or choose a new URI in both the upload and read prompt |
| Document upload HTTP 503 | Configure a nonempty model-provider key on the server in terminal 1, then restart it; a key set only in terminal 2 does not configure the server |
| Empty/invalid model section output | Inspect server logs and [section configuration](document-section-generation.md); successful fallback ingestion does not establish model section accuracy |

### Clean up the demo

1. Stop the demo server with Ctrl-C in terminal 1.
2. In terminal 2, from the repository root, run the commands below to remove the demo's dedicated database container and document directory. **These commands delete the demo data.** Skip them when using an existing service; if you changed `DOC_STORE_ROOT`, use the actual demo directory.

```bash
docker rm -f -v contexthub-deepagents-demo
rm -rf ./data/docs/deepagents-demo
```

## Validation record (2026-11-03)

- **Environment:** a fresh Python 3.13.9 environment, Docker PostgreSQL 16 with pgvector, and a non-superuser application role. Real model calls used DashScope `deepseek-v4-pro-0813` and `text-embedding-v4`.
- **Tutorial results:** both language versions passed independently against fresh demo databases, including memory save/recall/sharing, reads by the second agent, document upload/section reads, and invocation of the section 7 function.
- **Additional checks:** both real-model checks passed all eight check groups covering all ten tools, streaming, skill versions, authentication, and private/team/account isolation. The offline suite passed all 70 tests in the fresh environment.
- **Evidence:** [run summary](deepagents-tutorial-validation-20261103/summary.json), [English real-model report](deepagents-tutorial-validation-20261103/en/live-check/report.json), and [Chinese real-model report](deepagents-tutorial-validation-20261103/zh/live-check/report.json). Command output, server logs, and model traces are retained beside the reports.
- **Cleanup:** both test servers were stopped, and the dedicated containers/databases and temporary document stores were removed after verification. Times in the run summary use Hong Kong time (UTC+8); the live-check reports record UTC.

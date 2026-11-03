# ContextHub for Deep Agents

Connect an existing Deep Agent to ContextHub through native async tools. The
adapter calls the Python SDK directly; ContextHub continues to enforce its
account, agent, visibility, versioning, and sharing rules on the server.

New to the combined setup? Follow the
[Deep Agents + ContextHub tutorial](../../docs/setup/deepagents-integration-guide.md)
for a fresh demo server, model configuration, memory sharing, and document reads.

## Install

From the ContextHub repository root, in your agent's Python 3.11+ environment:

```bash
python -m pip install -e ./sdk -e ./plugins/deepagents
```

The adapter targets Deep Agents 0.7 and was tested with `deepagents==0.7.32`,
`langchain==1.4.8`, and `langchain-core==1.7.4`. Install the provider integration
for your chosen model as well (for example, `langchain-openai` for OpenAI).
The ContextHub server runs separately and retains its own Python requirements.

## Add to an agent

```python
from contexthub_sdk import ContextHubClient
from contexthub_deepagents import CONTEXTHUB_INSTRUCTIONS, create_contexthub_tools
from deepagents import create_deep_agent


async def answer(question: str, model, *, url, api_key, account_id, agent_id):
    async with ContextHubClient(
        url=url,
        api_key=api_key,
        account_id=account_id,
        agent_id=agent_id,
    ) as client:
        agent = create_deep_agent(
            model=model,
            tools=create_contexthub_tools(client),
            system_prompt="You are a research assistant.\n\n" + CONTEXTHUB_INSTRUCTIONS,
        )
        return await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": question}],
            }
        )
```

For an existing agent, append these tools to its `tools` list and add the optional
`CONTEXTHUB_INSTRUCTIONS` to your existing system prompt. Keep your team's model,
middleware, backend, checkpointer, and other Deep Agents configuration.

The caller owns the SDK client. Keep it open until `ainvoke` finishes or the
`astream` iterator is exhausted, and close it at application shutdown. Synchronous
`invoke` is not supported by these tools.

## Tools

| Tool | Purpose |
|---|---|
| `contexthub_search` | Search visible contexts, optionally by scope and context type; preserve source URIs, versions, retrieval IDs, and requested stale notices |
| `contexthub_ls` | List child context URIs |
| `contexthub_read` | Read L0/L1/L2 content or a specific skill version |
| `contexthub_stat` | Get context metadata, including its ID |
| `contexthub_sections` | List a long document's sections |
| `contexthub_read_section` | Read a section's original text |
| `contexthub_store` | Save a private memory |
| `contexthub_promote` | Promote a memory to a team |
| `contexthub_skill_publish` | Publish a new version of an existing skill |
| `contexthub_feedback` | Record whether retrieved context was useful |

All names have a `contexthub_` prefix so Deep Agents' built-in filesystem tools
keep their own names and meaning. To expose only selected operations, filter the
returned list by `tool.name` before passing it to the agent.

The OpenClaw adapter uses the same eight operation names, with framework-specific parameter schemas. Deep Agents additionally exposes `contexthub_sections` and `contexthub_read_section`. SDK method names and server HTTP paths remain unchanged; see the [integration comparison and API overview](../../docs/design%20and%20development/development-guide.md#agent-integrations).

Search results identify sources by URI. For a long document, call
`contexthub_stat(uri)` to obtain its `id`, then use that ID as `context_id` with
the section tools. The search result's `section_id`, when present, can be passed
directly to `contexthub_read_section` alongside that context ID.

The tools return JSON-compatible SDK responses. HTTP errors become LangChain
error tool messages, rather than an empty successful search. Transport errors
also become error messages; the adapter does not retry or hide programming errors.

## Team identities and state

Create a client and tool list per ContextHub agent identity. Credentials,
`account_id`, and `agent_id` are bound by application code and are not model tool
arguments. The identity must already have the appropriate memberships and access
in ContextHub; this adapter does not provision accounts or teams.

Deep Agents' default general-purpose subagent inherits the parent's tools and
therefore uses the same ContextHub identity. For a custom subagent with a separate
identity, create a separate client and pass its tool list in that subagent's
configuration. Keep both clients open for the entire run. Changing a LangGraph
thread ID does not change the ContextHub account or agent identity.

This release provides **explicit tool access**. The agent chooses when to search
and save; it does not port OpenClaw's automatic pre-turn recall or post-turn fact
capture. Deep Agents owns conversation state, compaction, and its filesystem.
ContextHub stores the shared context accessed through these tools. ContextHub
skills are read/published records here, not automatically mounted Deep Agents
`SKILL.md` directories. SQL execution and benchmark adapters are separate work.

Automatic recall/capture accuracy remains a separate evaluation question.
It requires measuring which facts are saved and recalled correctly across
conversations; passing explicit-tool checks does not establish that accuracy.

## Run the example

Start ContextHub following the repository setup guide, then configure the agent
environment with your existing ContextHub identity and model-provider credentials:

```bash
export CONTEXTHUB_URL=http://localhost:8000
export CONTEXTHUB_API_KEY=your-server-api-key
export CONTEXTHUB_ACCOUNT_ID=acme
export CONTEXTHUB_AGENT_ID=query-agent
# Set the credentials required by the provider chosen with --model.
python plugins/deepagents/examples/query.py \
  "Search ContextHub for the team's deployment procedure and cite the source." \
  --model openai:your-tool-capable-model
```

## Test

```bash
python -m pip install -e ./sdk -e './plugins/deepagents[dev]'
python -m pytest plugins/deepagents/tests
```

The repository's default `python -m pytest` collects only the main `tests/`
directory, so it does not require this optional adapter. Select the adapter suite
explicitly after installing its development dependencies. To run it alongside
the SDK regression tests from the repository root:

```bash
python -m pytest plugins/deepagents/tests tests/test_sdk.py
```

The adapter's async tests are explicitly marked and work with either the root
configuration or the plugin's own configuration; no `-c` override is needed.

The tests use the real SDK and Deep Agents graph with mocked HTTP responses and a
scripted model. They require neither a running database nor a model API key, and
verify tool execution, error handling, identity separation, document reads, and
memory/sharing operations. They do not measure model answer quality or replace a
live-server acceptance run.

### Live acceptance check

Use a disposable ContextHub database with migrations applied. Create two test
agents with write access to the same team. Both also need membership in the root
team (`path = ""`), with write access for the writer: the current server ingests
long documents under `ctx://resources/` in that root team's scope.

With `CONTEXTHUB_*` and provider credentials configured as above, run:

```bash
python plugins/deepagents/examples/live_check.py \
  --model openai:your-tool-capable-model \
  --team adapter-test --reader reader \
  --output /tmp/contexthub-deepagents-live
```

For an OpenAI-compatible provider, install `langchain-openai` and set
`OPENAI_BASE_URL` and `OPENAI_API_KEY`. The agent's model settings are independent
of the ContextHub server's chat/embedding configuration.

This opt-in script makes real model requests and writes synthetic memories, a
document, skill versions, and feedback. It verifies all ten tools through real
Deep Agents runs, `ainvoke` and `astream`, another member's read access, private
and cross-account isolation, and authentication error handling. It saves model
messages and `report.json` in the output directory and leaves test records for
inspection. Delete the disposable database and document-store directory after
review. Do not run the repository's DB integration suite against an existing
research database: its fixtures truncate business tables.

For document model settings and fallback behavior, see
[document section generation](../../docs/setup/document-section-generation.md).
The [tutorial](../../docs/setup/deepagents-integration-guide.md) describes the
demo server configuration and its background-worker limitation. A successful
document tool call alone does not establish model section accuracy.

## References

- [Deep Agents customization](https://docs.langchain.com/oss/python/deepagents/customization)
- [ContextHub SDK](../../sdk)
- [OpenClaw integration](../../bridge/README.md)

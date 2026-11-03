"""Check the adapter with a real model and disposable ContextHub test data.

Requires a running server, an existing writable team with two member agents,
and root-team membership for both agents (required by document ingestion/read),
CONTEXTHUB_* settings as in query.py, and model-provider credentials. This writes
synthetic memories, a document, skill versions, and feedback. Use a disposable
database; fixtures and server audit records are intentionally retained for review.
"""

import argparse
import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from contexthub_deepagents import CONTEXTHUB_INSTRUCTIONS, create_contexthub_tools
from contexthub_sdk import ContextHubClient, ContextLevel, ContextType, Scope
from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langchain_core.messages import ToolMessage


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def tool_payload(messages, name):
    return json.loads(
        next(
            m.content for m in messages if isinstance(m, ToolMessage) and m.name == name
        )
    )


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex[:12]
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "model": args.model,
        "versions": {
            name: version(name)
            for name in [
                "deepagents",
                "langchain",
                "langchain-core",
                "contexthub-sdk",
                "contexthub-deepagents",
            ]
        },
        "checks": [],
    }
    model = init_chat_model(args.model, temperature=0, timeout=90, max_retries=0)
    account = os.environ["CONTEXTHUB_ACCOUNT_ID"]
    writer = os.environ["CONTEXTHUB_AGENT_ID"]
    require(writer != args.reader, "Reader must use a different agent identity")
    defaults = {
        "url": os.environ.get("CONTEXTHUB_URL", "http://localhost:8000"),
        "api_key": os.environ["CONTEXTHUB_API_KEY"],
        "account_id": account,
        "timeout": 120,
    }

    async def agent_check(name, client, prompt, required_tools, *, stream=False):
        started = time.monotonic()
        agent = create_deep_agent(
            model=model,
            tools=create_contexthub_tools(client),
            system_prompt=CONTEXTHUB_INSTRUCTIONS,
        )
        inputs = {"messages": [{"role": "user", "content": prompt}]}
        if stream:
            async for result in agent.astream(
                inputs, config={"recursion_limit": 30}, stream_mode="values"
            ):
                pass
        else:
            result = await agent.ainvoke(inputs, config={"recursion_limit": 30})
        messages = result["messages"]
        (args.output / f"{name}.json").write_text(
            json.dumps(
                [m.model_dump(mode="json") for m in messages],
                indent=2,
                ensure_ascii=False,
            )
        )
        calls = [m for m in messages if isinstance(m, ToolMessage)]
        require(
            required_tools <= {m.name for m in calls},
            f"{name}: missing required tool calls",
        )
        if name != "unauthorized":
            require(
                all(m.status != "error" for m in calls),
                f"{name}: tool returned an error",
            )
        report["checks"].append(
            {
                "name": name,
                "tools": [m.name for m in calls],
                "seconds": round(time.monotonic() - started, 2),
            }
        )
        print(f"{name}: model completed; checking results", flush=True)
        return messages

    try:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                ContextHubClient(**defaults, agent_id=writer)
            )
            reader = await stack.enter_async_context(
                ContextHubClient(**defaults, agent_id=args.reader)
            )
            topic = f"Aurora deployment {run_id}"
            code = "DEPLOY-" + uuid4().hex[:10]
            seed = await client.memory.add(
                content=f"{topic}. The release approval code is {code}. Release window: Tuesday 09:30 UTC.",
                tags=[run_id],
            )
            messages = await agent_check(
                "search_read_feedback",
                client,
                f"Search ContextHub for '{topic}', read its full source, and tell me its release approval code and window. Cite the URI and report adopted feedback with the retrieval ID.",
                {"contexthub_search", "contexthub_read", "contexthub_feedback"},
            )
            require(
                code in messages[-1].text and seed.uri in messages[-1].text,
                "Answer must contain the stored code and source URI",
            )
            search = tool_payload(messages, "contexthub_search")
            feedback = tool_payload(messages, "contexthub_feedback")
            require(
                feedback["retrieval_id"] == search["retrieval_id"],
                "Feedback must retain the search retrieval ID",
            )

            shared_text = f"Shared rollout {run_id}: freeze duration is 17 minutes."
            messages = await agent_check(
                "store_promote",
                client,
                f"Save this exact text as a private ContextHub memory, then promote the new memory to team '{args.team}': {shared_text}",
                {"contexthub_store", "contexthub_promote"},
            )
            private = tool_payload(messages, "contexthub_store")
            shared = tool_payload(messages, "contexthub_promote")
            require(
                private["owner_space"] == writer and shared["owner_space"] == args.team,
                "Stored identities must match the configured client and team",
            )
            require(
                (await reader.read(shared["uri"], level=ContextLevel.L2)).content
                == shared_text,
                "The second team member must read the exact shared text",
            )

            async def denied(check_name, other_client, uri, expected_status):
                tool = next(
                    t
                    for t in create_contexthub_tools(other_client)
                    if t.name == "contexthub_read"
                )
                result = await tool.ainvoke(
                    {
                        "name": tool.name,
                        "args": {"uri": uri, "level": "L2"},
                        "id": check_name,
                        "type": "tool_call",
                    }
                )
                require(
                    result.status == "error"
                    and f"HTTP {expected_status}:" in result.content,
                    check_name,
                )
                require(
                    code not in result.content and shared_text not in result.content,
                    "Denied reads must not expose source text",
                )
                report["checks"].append({"name": check_name, "status": expected_status})

            await denied("private_memory_isolation", reader, seed.uri, 403)
            outsider = await stack.enter_async_context(
                ContextHubClient(**defaults, agent_id=f"outsider-{run_id}")
            )
            await denied("team_membership_isolation", outsider, shared["uri"], 403)
            other_account = await stack.enter_async_context(
                ContextHubClient(
                    **(defaults | {"account_id": f"other-{run_id}"}), agent_id=writer
                )
            )
            await denied("account_isolation", other_account, seed.uri, 404)

            doc_uri = f"ctx://resources/live-{run_id}"
            doc_code = "RECOVERY-" + uuid4().hex[:10]
            document = args.output / "fixture.md"
            document.write_text(
                f"# Rollout handbook {run_id}\n\n## Release\n\nRelease starts on Tuesday.\n\n## Recovery\n\nThe recovery approval code is {doc_code}. Pause the rollout for 23 minutes.\n"
            )
            await client.document.ingest(doc_uri, str(document), tags=[run_id])
            messages = await agent_check(
                "document_stream",
                reader,
                f"List ContextHub documents under ctx://resources. For {doc_uri}, inspect its metadata, list its sections and read the Recovery section's original text. What is the recovery code and pause duration? Cite the document URI.",
                {
                    "contexthub_ls",
                    "contexthub_stat",
                    "contexthub_sections",
                    "contexthub_read_section",
                },
                stream=True,
            )
            require(
                doc_code in messages[-1].text
                and doc_uri in messages[-1].text
                and "23" in messages[-1].text,
                "Streamed answer must use the document section",
            )

            skill_uri = f"ctx://team/{args.team}/skills/live-{run_id}"
            await client.context.create(
                uri=skill_uri,
                context_type=ContextType.SKILL,
                scope=Scope.TEAM,
                owner_space=args.team,
                l2_content="Live fixture",
            )
            old = await client.skill.publish(
                skill_uri=skill_uri, content="Old procedure: wait 5 minutes."
            )
            new_content = "New procedure: wait 11 minutes."
            messages = await agent_check(
                "skill_publish",
                client,
                f"Publish a new version of {skill_uri}. The content argument must "
                f"be exactly the string encoded by this JSON value:\n"
                f"{json.dumps(new_content)}\n\n"
                f"After publishing, read both the new version and the old version "
                f"{old.version} to confirm the old content remains available. "
                "These verification instructions are not part of the skill content.",
                {"contexthub_skill_publish", "contexthub_read"},
            )
            published = tool_payload(messages, "contexthub_skill_publish")
            require(
                published["version"] == old.version + 1,
                "Skill version must advance once",
            )
            require(
                (await reader.read(skill_uri, version=old.version)).content
                == old.content,
                "Old skill version must remain intact",
            )
            require(
                (await reader.read(skill_uri, version=published["version"])).content
                == new_content,
                "New skill content must persist",
            )

            invalid = await stack.enter_async_context(
                ContextHubClient(
                    **(defaults | {"api_key": "deliberately-invalid-test-key"}),
                    agent_id=writer,
                )
            )
            messages = await agent_check(
                "unauthorized",
                invalid,
                f"Try reading {seed.uri}. If the server refuses authentication, tell me and stop.",
                {"contexthub_read"},
            )
            errors = [m for m in messages if isinstance(m, ToolMessage)]
            require(
                all(m.status == "error" and "HTTP 401:" in m.content for m in errors),
                "Authentication errors must reach the model",
            )
            require(
                code not in messages[-1].text,
                "Unauthorized agent must not see the private content",
            )
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", required=True, help="LangChain provider:model identifier"
    )
    parser.add_argument(
        "--team", required=True, help="Existing writable ContextHub team"
    )
    parser.add_argument(
        "--reader", required=True, help="Another existing member of that team"
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Directory for synthetic fixtures, model traces, and report.json",
    )
    asyncio.run(run(parser.parse_args()))

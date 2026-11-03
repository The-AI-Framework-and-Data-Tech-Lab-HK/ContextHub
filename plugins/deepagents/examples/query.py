"""Run a Deep Agent against a running ContextHub server."""

import argparse
import asyncio
import os

from contexthub_deepagents import CONTEXTHUB_INSTRUCTIONS, create_contexthub_tools
from contexthub_sdk import ContextHubClient
from deepagents import create_deep_agent


async def run(question: str, model: str) -> None:
    async with ContextHubClient(
        url=os.environ.get("CONTEXTHUB_URL", "http://localhost:8000"),
        api_key=os.environ["CONTEXTHUB_API_KEY"],
        account_id=os.environ["CONTEXTHUB_ACCOUNT_ID"],
        agent_id=os.environ["CONTEXTHUB_AGENT_ID"],
    ) as client:
        agent = create_deep_agent(
            model=model,
            tools=create_contexthub_tools(client),
            system_prompt=CONTEXTHUB_INSTRUCTIONS,
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}
        )
        print(result["messages"][-1].text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument(
        "--model", required=True, help="LangChain provider:model identifier"
    )
    args = parser.parse_args()
    asyncio.run(run(args.question, args.model))

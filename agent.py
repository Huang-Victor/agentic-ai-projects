import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic
from duckduckgo_search import DDGS

load_dotenv()

client = Anthropic()
ddgs = DDGS()


def search_web(query: str) -> str:
    """Search the web using DuckDuckGo and return the top results."""
    results = ddgs.text(query, max_results=3)
    output = []
    for r in results:
        output.append(f"Title: {r['title']}\nURL: {r['href']}\nSnippet: {r['body']}\n")
    return "\n".join(output)


# Define the tool for Claude
tools = [
    {
        "name": "search_web",
        "description": "Search the web for current information on a topic. Use this when you need to find facts, recent events, or information you're not confident about.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to look up"
                }
            },
            "required": ["query"]
        }
    }
]

def run_agent(user_question):
    """RUn the agent loop: send questions to Claude, handle tool calls, return final answer."""
    print(f"\nUser: {user_question}")
    
    #Start the conversation with the user's question
    messages = [
        {"role": "user", "content": user_question}
    ]

    # Step 1: Send the question to Claude along with the tool definitions
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=tools,
        messages=messages
    )

    print(f"\nClaude's stop reason: {response.stop_reason}")

    # Step 2: Loop until Claude gives a final answer (not a tool call)
    while response.stop_reason == "tool_use":

        # Step 3: Find the tool call in Claude's response
        tool_use_block = None
        for block in response.content:
            if block.type == "tool_use":
                tool_use_block = block
                break
        
        tool_name = tool_use_block.name
        tool_input = tool_use_block.input

        print(f"\nClaude wants to call: {tool_name}")
        print(f"With input: {tool_input}")

        #Step 4: Actually run the tool (this is the PAUSE -> Observation handoff)
        if tool_name == "search_web":
            tool_result = search_web(tool_input["query"])
        else:
            tool_result = f"ErrorL Unknown tool {tool_name}"

        print(f"\nTool result preview: {tool_result[:200]}...")

        # Step 5: Feed the result back to Claude
        # We need to send the FULL conversation: original question + Claude's response + tool result
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_block.id,
                    "content": tool_result
                }
            ]
        }) 

        # Step 6: Let Claude process the tool result and either answer or call another tool
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=tools,
            messages=messages
        )

        print(f"\nClaude's stop reason: {response.stop_reason}")

    # Step 7: Extract and return the final text answer
    final_answer = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_answer += block.text
            
    print(f"\nClaude: {final_answer}")
    return final_answer

if __name__ == "__main__":
    run_agent("What is the current weather in New York City?")
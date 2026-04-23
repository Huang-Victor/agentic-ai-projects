import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic
from ddgs import DDGS

load_dotenv()

client = Anthropic()
ddgs = DDGS()


def search_web(query: str) -> str:
    """Search the web using DuckDuckGo and return the top results."""
    results = ddgs.text(query, max_results=3)
    output = []
    for r in results:
        output.append(f"Title: {r['title']}\nURL: {r['href']}\nSnippet: {r['body']}\n")

    print(f"DEBUG - Number of results: {len(output)}")
    print(f"DEBUG - Raw results: {results}")
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

SYSTEM_PROMPT = """You are a research assistant that thoroughly investigates questions before answering.

When given a question:
1. Think about what information you need to answer it well
2. Search for that information
3. Evaluate what you found - is it enough to give a complete, accurate answer?
4. If not, identify what's missing and search again with a more specific query
5. Once you have sufficient information, provide a comprehensive answer grounded in what you found

Always cite specific facts from your search results. Never make up information.
If your searches don't return useful results, say so honestly rather than guessing."""

def run_agent(user_question, max_turns=5):
    """RUn the agent loop: send questions to Claude, handle tool calls, return final answer."""
    print(f"\nUser: {user_question}")
    
    #Start the conversation with the user's question
    messages = [
        {"role": "user", "content": user_question}
    ]

    turn = 0

    # Step 1: Send the question to Claude along with the tool definitions
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=messages
    )

    print(f"\n--- turn {turn + 1} ---")
    print(f"\nClaude's stop reason: {response.stop_reason}")

    # Step 2: Loop until Claude gives a final answer (not a tool call)
    while response.stop_reason == "tool_use":
        turn += 1

        if turn >= max_turns:
            print(f"\nMax turns ({max_turns}) reached. Stopping.")
            break

# Step 3: Find ALL tool calls in Claude's response
        tool_use_blocks = []
        for block in response.content:
            if block.type == "tool_use":
                tool_use_blocks.append(block)
        
        print(f"Claude wants to make {len(tool_use_blocks)} tool call(s)")
        
        # Step 4: Run ALL tool calls and collect results
        tool_results = []
        for tool_use_block in tool_use_blocks:
            tool_name = tool_use_block.name
            tool_input = tool_use_block.input
            
            print(f"Calling: {tool_name} with input: {tool_input}")
            
            if tool_name == "search_web":
                tool_result = search_web(tool_input["query"])
            else:
                tool_result = f"Error: Unknown tool {tool_name}"
            
            print(f"Result preview: {tool_result[:150]}...")
            
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_block.id,
                "content": tool_result
            })
        
        # Step 5: Feed ALL results back in one message
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": tool_results
        })

        # Step 6: Let Claude process the tool result and either answer or call another tool
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages
        )

        print(f"\n--- Turn {turn + 1}---")
        print(f"\nClaude's stop reason: {response.stop_reason}")

    # Step 7: Extract and return the final text answer
    final_answer = ""
    for block in response.content:
        if hasattr(block, "text"):
            final_answer += block.text
            
    print(f"\nClaude: {final_answer}")
    return final_answer

if __name__ == "__main__":
    run_agent("Compare the population and cost of living between New York City, New York and San Francisco, California. Which city would be better for a young software engineer?")
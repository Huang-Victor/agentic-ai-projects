import os
import json
import requests
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
notion = Client(auth=os.getenv("NOTION_API_KEY"), notion_version="2022-06-28")

# Create a fresh database for this test.
db = notion.databases.create(
    parent={"type": "page_id", "page_id": "34cb6cf3b46980c9ab00d8896467fa30"},
    title=[{"type": "text", "text": {"content": "Archive test"}}],
    properties={"Name": {"title": {}}},
)
db_id = db["id"]
print(f"Created test database: {db_id}")

# Step 1: retrieve the database and inspect its actual shape.
print("\n--- Retrieving database state ---")
retrieved = notion.databases.retrieve(database_id=db_id)
print(json.dumps(retrieved, indent=2))

# Step 2: try archiving via raw HTTP, bypassing the SDK entirely.
print("\n--- Attempting raw HTTP archive ---")
headers = {
    "Authorization": f"Bearer {os.getenv('NOTION_API_KEY')}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
response = requests.patch(
    f"https://api.notion.com/v1/databases/{db_id}",
    headers=headers,
    json={"archived": True},
)
print(f"HTTP status: {response.status_code}")
print(f"Response body: {response.text}")

# Step 3: re-retrieve to see whether archive took effect.
print("\n--- Retrieving database state after archive attempt ---")
retrieved_after = notion.databases.retrieve(database_id=db_id)
print(f"archived: {retrieved_after.get('archived')}")
print(f"in_trash: {retrieved_after.get('in_trash')}")
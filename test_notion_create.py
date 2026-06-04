import os
import json
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
notion = Client(
    auth=os.getenv("NOTION_API_KEY"),
    notion_version="2022-06-28",
)

response = notion.databases.create(
    parent={"type": "page_id", "page_id": "34cb6cf3b46980c9ab00d8896467fa30"},
    title=[{"type": "text", "text": {"content": "API test database 2"}}],
    properties={
        "Name": {"title": {}},
        "Priority": {"select": {"options": [
            {"name": "High"},
            {"name": "Medium"},
            {"name": "Low"},
        ]}},
        "Due Date": {"date": {}},
    },
)

print("Full response:")
print(json.dumps(response, indent=2))
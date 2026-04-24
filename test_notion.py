import os
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

notion = Client(auth=os.environ["NOTION_API_KEY"])

# Test: fetch the page to confirm connection and permissions
page = notion.pages.retrieve(page_id=os.environ["NOTION_PAGE_ID"])
print(f"Successfully connected to Notion!")
print(f"Page title: {page['properties']}")
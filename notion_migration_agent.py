from typing import TypedDict, Any
import uuid
from datetime import datetime
import csv
import os
from dateutil.parser import parse as parse_date
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

class StructuralValidationResult(TypedDict):
    valid: bool
    error_messages: list[str]

class SuspiciousValue(TypedDict):
    value: str
    reason: str
    

class ColumnProfile(TypedDict):
    column_name: str
    inferred_type: str
    type_reasoning: str           # new — empty string until Phase 2 fills it
    null_count: int
    distinct_count: int
    distinct_values_sample: list[str]
    suspicious_values: list[SuspiciousValue]   # changed from list[str]
    example_rows: list[int]


class NotionProperty(TypedDict):
    name: str
    type: str
    options: list[str] | None
    confidence: str


class MappingEntry(TypedDict):
    csv_column: str
    notion_property: str
    confidence: str


class EditAction(TypedDict):
    action: str
    target: str
    details: dict[str, Any]
    timestamp: str


class ColumnCoercionResult(TypedDict):
    property_name: str
    target_type: str
    success_count: int
    failure_count: int
    failure_examples: list[str]


class SampleSelection(TypedDict):
    row_index: int
    selection_reason: str


class RowOutcome(TypedDict):
    row_index: int
    success: bool
    notion_url: str | None
    error_message: str | None


class ReconciliationReport(TypedDict):
    total_rows: int
    succeeded: int
    failed: int
    failure_patterns: list[str]
    suggested_fixes: list[str]
    failed_row_indices: list[int]


class MigrationState(TypedDict):
    run_id: str
    source_csv_path: str
    target_notion_parent_id: str
    started_at: str
    structural_validation_result: StructuralValidationResult
    profile: list[ColumnProfile]
    pre_edit_schema: list[NotionProperty]
    pre_edit_mapping: list[MappingEntry]
    edit_history: list[EditAction]
    post_edit_schema: list[NotionProperty]
    post_edit_mapping: list[MappingEntry]
    coercion_report: list[ColumnCoercionResult]
    sample_selection: list[SampleSelection]
    notion_database_id: str | None
    sample_row_outcomes: list[RowOutcome]
    bulk_row_outcomes: list[RowOutcome]
    rejection_return_stage: str | None
    reconciliation_report: ReconciliationReport | None

def init_state(csv_path: str, parent_id: str) -> MigrationState:
    return {
        "run_id": str(uuid.uuid4()),
        "source_csv_path": csv_path,
        "target_notion_parent_id": parent_id,
        "started_at": datetime.now().isoformat(),
        "structural_validation_result": {"valid": False, "error_messages": []},
        "profile": [],
        "pre_edit_schema": [],
        "pre_edit_mapping": [],
        "edit_history": [],
        "post_edit_schema": [],
        "post_edit_mapping": [],
        "coercion_report": [],
        "sample_selection": [],
        "notion_database_id": None,
        "sample_row_outcomes": [],
        "bulk_row_outcomes": [],
        "rejection_return_stage": None,
        "reconciliation_report": None,
    }

def structural_validation_node(state: MigrationState) -> MigrationState:
    """Validate that the CSV is parseable and structurally well-formed."""
    print(f"\n--- STRUCTURAL VALIDATION ---")
    path = state["source_csv_path"]
    print(f"Source CSV File Path: {path}")

    errors: list[str] = []

    # Check 1: File exists
    if not os.path.exists(path):
        errors.append(f"File does not exist at the given path: {path}")
        # Without a file, no further checks are possible. Bail to the write step.
        state["structural_validation_result"] = {
            "valid": False,
            "error_messages": errors,
        }
        return state
    
    # Check 2: file is non-empty
    if os.path.getsize(path) == 0:
        errors.append("File is empty (0 bytes).")
        state["structural_validation_result"] = {
            "valid": False,
            "error_messages": errors,
        }
        return state
    
    # Check 3: file is readable as UTF-8 text
    # This catches the .xlsx-renamed-to-.csv case as well as wrong-encoding files.
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except UnicodeDecodeError:
        errors.append(
            "File is not valid UTF-8. It may be saved in a different encoding " \
            "(e.g. Windows-1252) or it may be a binary file (e.g. .xlsx) " \
            "renamed to .csv. Please re-export as UTF-8 CSV."
        ) 
        state["structural_validation_result"] = {
            "valid": False,
            "error_messages": errors,
        }
        return state
    
    # Check 4: Header row exists with at least one non-empty column name
    if len(rows) == 0:
        errors.append("Files contains no rows at all (not even a header).")
    else:
        header = rows[0]
        if len(header) == 0 or all(cell.strip() == "" for cell in header):
            errors.append("Header row is missing or has no column names.")

    # Check 5: at least one data row exists
    if len(rows) < 2:
        errors.append("File has a header but no data rows.")

    # Check 6: every data row has the same column count as the header
    if len(rows) >= 2:
        expected_count = len(rows[0])
        for i, row in enumerate(rows[1:], start=2): # start=2 because row 1 is the header, data starts at row 2
            if len(row) != expected_count:
                errors.append(
                    f"Row {i} has {len(row)} columns; header has {expected_count}."
                )

    # Write result to state - once, at the end
    state["structural_validation_result"] = {
        "valid": len(errors) == 0,
        "error_messages": errors,
    }
    return state

def profile_node(state:MigrationState) -> MigrationState:
    """1. Build a deterministic profile of the input data.
    2. LLM determines if certain suspicious values are correct"""
    print(f"\n--- PROFILE ---")
    path = state["source_csv_path"]

    # Load the CSV.We already know its structurally valid because the 
    # orchestrator only calls this node when validation passed.
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    header = rows[0]
    data_rows = rows[1:]

    profile: list[ColumnProfile] = []

    for col_index, col_name in enumerate(header):
        # Extract every value in this column.
        column_values = [row[col_index] for row in data_rows]

        # Compute stats on this column.
        column_profile = profile_column(col_name, column_values)
        profile.append(column_profile)

    for column_profile in profile:
        enriched = enrich_column_profile_with_llm(column_profile)
        column_profile["inferred_type"] = enriched["inferred_type"]
        column_profile["type_reasoning"] = enriched["type_reasoning"]
        column_profile["suspicious_values"].extend(enriched["suspicious_values"])

    state["profile"] = profile
    return state

    
def profile_column(col_name: str, values: list[str]) -> ColumnProfile:
    """Compute deterministic stats for a single column."""

    # 1. Null count (empty or whitespace-only)
    null_count = 0
    for value in values:
        if not value.strip():
            null_count += 1

    # 2. Non-null values for everything else
    non_null = [v for v in values if v.strip() != ""]

    # 3. Distinct values (using set for O(1) membership)
    distinct_set = set(non_null)
    distinct_count = len(distinct_set)

    # 4. Distinct values sample (capped at 20)
    distinct_values_sample = list(distinct_set)[:20]

    # 5. Mechanical type check
    inferred_type, suspicious = guess_mechanical_type(non_null)

    # 6. Example row indices (first 5 non-null rows)
    example_rows = [
        i for i, v in enumerate(values, start=1) if v.strip() != ""
    ][:5]

    return {
        "column_name": col_name,
        "inferred_type": inferred_type,
        "type_reasoning": "",
        "null_count": null_count,
        "distinct_count": distinct_count,
        "distinct_values_sample": distinct_values_sample,
        "suspicious_values": suspicious,
        "example_rows": example_rows,
    }


def guess_mechanical_type(non_null_values: list[str]) -> tuple[str, list[SuspiciousValue]]:
    """
    Try to classify a column by checking how many values match each candidate type.
    Returns (inferred_type, suspicious_values).
    
    A type "wins" if at least 95% of non-null values pass its check.
    The values that fail the winning type's check are the suspicious values.
    If no type wins, return ('unknown', []) — Phase 2 will reason about it.
    """
    if not non_null_values:
        return "unknown", []

    # Define the per-value checks for each candidate type.
    def is_number(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    def is_boolean(s: str) -> bool:
        return s.strip().lower() in {"true", "false", "yes", "no", "y", "n", "0", "1"}

    def is_date(s: str) -> bool:
        # Use dateutil for forgiving date parsing
        
        try:
            parse_date(s)
            return True
        except (ValueError, TypeError):
            return False

    candidates = [
        ("number",  is_number),
        ("boolean", is_boolean),
        ("date",    is_date),
    ]
    
    threshold = 0.95
    
    for type_name, check in candidates:
        passes = [v for v in non_null_values if check(v)]
        match_ratio = len(passes) / len(non_null_values)
        if match_ratio >= threshold:
            failures = [
                {"value": v, "reason": f"did not parse as {type_name}"}
                for v in non_null_values
                if not check(v)
            ]
            return type_name, failures

    return "unknown", []


def enrich_column_profile_with_llm(profile: ColumnProfile) -> dict:
    """Phase 2: ask the LLM to refine the column's type and identify semantic issues."""

    prompt = f"""You are analyzing a single column from a CSV that will be migrated to a Notion database.
    Based on the profile below, decide the most appropriate Notion property type and identify any suspicious values.

    Column profile:
    - Column name: {profile["column_name"]}
    - Mechanical type guess (from deterministic checks): {profile["inferred_type"]}
    - Total distinct values: {profile["distinct_count"]}
    - Null count: {profile["null_count"]}
    - Sample of distinct values: {profile["distinct_values_sample"]}

    Notion property types to choose from: title, rich_text, select, multi_select, date, number, checkbox, email, url, phone_number.

    Identify suspicious values: typos, casing inconsistencies, format inconsistencies, or values that don't fit the column's apparent purpose. For each, give the value and a short reason.

    If the column has only a few distinct values relative to total rows, it is probably a select.
    If the column has many distinct values that all look similar in shape, it is probably rich_text.
    """

    enrichment_tool = {
        "name": "submit_column_enrichment",
        "description": "Submit the refined column type and any suspicious values found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inferred_type": {
                    "type": "string",
                    "description": "Notion property type for this column.",
                },
                "type_reasoning": {
                    "type": "string",
                    "description": "Brief justification for the chosen type.",
                },
                "suspicious_values": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["value", "reason"],
                    },
                },
            },
            "required": ["inferred_type", "type_reasoning", "suspicious_values"],
        },
    }

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        tools=[enrichment_tool],
        tool_choice={"type": "tool", "name": "submit_column_enrichment"},
        messages=[{"role": "user", "content": prompt}],
    )

    # The tool input is the structured response — read it directly, no parsing needed.
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_column_enrichment":
            return block.input

    # Fallback: LLM didn't use the tool (very rare with tool_choice forced)
    return {
        "inferred_type": profile["inferred_type"],
        "type_reasoning": "LLM enrichment failed; preserving Phase 1 guess.",
        "suspicious_values": [],
    }



if __name__ == "__main__":
    state = init_state(
        csv_path="Test Files/stress_test_2.csv",
        parent_id="34cb6cf3b46980c9ab00d8896467fa30",
    )
    state = structural_validation_node(state)
    
    if state["structural_validation_result"]["valid"]:
        state = profile_node(state)
        from pprint import pp
        print("\nProfile:")
        pp(state["profile"])
    else:
        print("\nValidation failed; skipping profile.")
        for msg in state["structural_validation_result"]["error_messages"]:
            print(f"  - {msg}")
from typing import TypedDict, Any
import uuid
from datetime import datetime
import csv
import os
from dateutil.parser import parse as parse_date
from anthropic import Anthropic
from dotenv import load_dotenv
import shlex
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError, RequestTimeoutError, HTTPResponseError
import requests

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
notion = NotionClient(
    auth=os.getenv("NOTION_API_KEY"),
    notion_version="2022-06-28",
)

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
    failure_examples: list[CoercionFailure]  # was list[str]


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


class CoercionFailure(TypedDict):
    row_index: int
    value: str
    reason: str


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

def schema_and_mapping_node(state: MigrationState) -> MigrationState:
    """Build the proposed Notion schema and CSV-to-property mapping from the profile."""
    print(f"\n--- SCHEMA AND MAPPING PROPOSAL ---")

    schema: list[NotionProperty] = []
    mapping: list[MappingEntry] = []

    for column_profile in state["profile"]:
        notion_property = build_property_from_profile(column_profile)
        mapping_entry = build_mapping_from_profile(column_profile, notion_property["name"])

        schema.append(notion_property)
        mapping.append(mapping_entry)

    state["pre_edit_schema"] = schema
    state["pre_edit_mapping"] = mapping
    return state

def build_property_from_profile(column_profile: ColumnProfile) -> NotionProperty:
    """Turn one ColumnProfile into one NotionProperty."""

    # 1. Prettify the name: due_date -> Due Date
    property_name = column_profile["column_name"].replace("_", " ").title()

    # 2. Type comes straight from Phase 2's inferred_type
    property_type = column_profile["inferred_type"]

    # 3. Options apply only to select / multi_select
    if property_type in {"select", "multi_select"}:
        suspicious_value_strings = {
            sv["value"] for sv in column_profile["suspicious_values"]
        }
        distinct_values = set(column_profile["distinct_values_sample"])
        canonical_options = sorted(distinct_values - suspicious_value_strings)
        options: list[str] | None = canonical_options
    else:
        options = None

    # 4. Confidence rating, derived from the profile
    if property_type == "unknown":
        confidence = "low"
    elif len(column_profile["suspicious_values"]) > 0:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "name": property_name,
        "type": property_type,
        "options": options,
        "confidence": confidence,
    }


def build_mapping_from_profile(column_profile: ColumnProfile, property_name: str) -> MappingEntry:
    """Map a CSV column to its Notion property."""

    # Mirror confidence from the schema's own logic.
    if column_profile["inferred_type"] == "unknown":
        confidence = "low"
    elif len(column_profile["suspicious_values"]) > 0:
        confidence = "medium"
    else:
        confidence = "high"

    return {
        "csv_column": column_profile["column_name"],
        "notion_property": property_name,
        "confidence": confidence,
    }

def human_checkpoint_node(state: MigrationState) -> MigrationState:
    """First human checkpoint. Human reviews and edits schema + mapping proposals."""
    print(f"\n--- HUMAN CHECKPOINT 1 ---")

    # Work on copies of the proposals - preserve the originials for audit. 
    working_schema: list[NotionProperty] = [dict(p) for p in state["pre_edit_schema"]]
    working_mapping: list[MappingEntry] = [dict(m) for m in state["pre_edit_mapping"]]

    print_checkpoint_context(state["profile"], working_schema, working_mapping)
    print_command_help()

    while True:
        try:
            raw_input = input("\n> ").strip()
        except EOFError:
            # Stdin closed (rare in interactive use). Treat as abort).
            print("Input stream closed. Aborting.")
            state["rejection_return_stage"] = "human_checkpoint_1"
            return state
        
        if not raw_input:
            continue

        try:
            tokens = shlex.split(raw_input)
        except ValueError as e:
            print(f"Couldn't parse input: {e}")
            continue

        command = tokens[0].lower()
        args = tokens[1:]

        if command == "enter":
            state["post_edit_schema"] = working_schema
            state["post_edit_mapping"] = working_mapping
            print("Approved. Continuing pipeline.")
            return state
        
        elif command == "abort":
            print("Aborting run.")
            state["rejection_return_stage"] = "human_checkpoint_1"
            return state
        
        elif command == "help":
            print_command_help()

        elif command == "preview":
            print_working_state(working_schema, working_mapping)

        elif command == "remove":
            apply_remove(args, working_schema, working_mapping, state)

        elif command == "add":
            apply_add(args, working_schema, state)

        elif command == "edit_type":
            apply_edit_type(args, working_schema, state)

        elif command == "edit_options":
            apply_edit_options(args, working_schema, state)

        elif command == "edit_mapping":
            apply_edit_mapping(args, working_schema, working_mapping, state)

        elif command == "remove_mapping":
            apply_remove_mapping(args, working_mapping, state)

        else:
            print(f"Unknown command: {command!r}. Type 'help' for a list.")

def print_checkpoint_context(
        profile: list[ColumnProfile],
        schema: list[NotionProperty],
        mapping: list[MappingEntry],
) -> None:
    """Show the human what they're reviewing."""
    from pprint import pp
    print("\nProfile (evidence):")
    pp(profile)
    print("\nProposed schema:")
    pp(schema)
    print("\nProposed mapping:")
    pp(mapping)

def print_working_state(
        schema: list[NotionProperty],
        mapping: list[MappingEntry],
) -> None:
    """Show the current state of the working copies."""
    from pprint import pp
    print("\nCurrent working schema:")
    pp(schema)
    print("\nCurrent working mapping:")
    pp(mapping)

def print_command_help() -> None:
    print("""
Available commands:
  preview                                       — show current working schema and mapping
  enter                                         — approve and continue
  abort                                         — discard and halt run
  remove <property_name>                        — remove a property (use quotes if name contains spaces)
  add <property_name> <type>                    — add a new property
  edit_type <property_name> <new_type>          — change a property's type
  edit_options <property_name> <opt1,opt2,...>  — set select options
  edit_mapping <csv_column> <property_name>     — change which property a CSV column maps to
  remove_mapping <csv_column>                   — drop a mapping (property stays)
  help                                          — print this list
""")
    
def find_property(name: str, schema: list[NotionProperty]) -> NotionProperty | None: 
    """Look up a property by display name. Case-insensitive."""
    for prop in schema:
        if prop["name"].lower() == name.lower():
            return prop
    return None

def record_edit(
        state: MigrationState,
        action: str,
        target: str,
        details: dict[str, Any],
) -> None:
    """Append an EditAction record to edit_history."""
    state["edit_history"].append({
        "action": action,
        "target": target,
        "details": details,
        "timestamp": datetime.now().isoformat(),
    })

def apply_remove(
        args: list[str],
        schema: list[NotionProperty],
        mapping: list[MappingEntry],
        satate: MigrationState,
) -> None:
    if len(args) != 1:
        print("Usage: remove <property_name>")
        return
    
    name = args[0]
    prop = find_property(name, schema)
    if prop is None:
        print(f"No property named {name!r}.")
        return
    
    # Remove the property.
    schema.remove(prop)
    print(f"Removed property {prop['name']!r}.")

    # Cascade: remove any mappings that pointed at this property.
    cascaded = [m for m in mapping if m["notion_property"].lower() == prop["name"].lower()]
    for m in cascaded:
        mapping.remove(m)
        print(f"  Also removed mapping: {m['csv_column']!r} → {m['notion_property']!r}.")

    record_edit(state, "remove_property", prop["name"], {
        "cascaded_mappings_removed": [m["csv_column"] for m in cascaded],
    })

def apply_add(
        args: list[str],
        schema: list[NotionProperty],
        state: MigrationState, 
) -> None:
    if len(args) != 2:
        print("Usage: add <property_name> <type>")
        return
    
    name, prop_type = args
    if find_property(name, schema) is not None:
        print(f"Property {name!r} already exists.")
        return
    
    new_prop: NotionProperty = {
        "name": name,
        "type": prop_type,
        "options": None,
        "confidence": "low", # human-added properties start at low confidence - no profile backing
    }
    schema.append(new_prop)
    print(f"Added property {name!r} of type {prop_type!r}.")
    record_edit(state, "add_property", name, {"type": prop_type})

def apply_edit_type(
        args: list[str],
        schema: list[NotionProperty],
        state: MigrationState,
) -> None:
    if len(args) != 2:
        print("Usage: edit_type <property_name> <new_type>")
        return
    
    name, new_type = args
    prop = find_property(name, schema)
    if prop is None:
        print(f"No property named {name!r}.")
        return
    
    old_type = prop["type"]
    prop["type"] = new_type
    # If changing away from select/multi_select, options no longer apply.
    if new_type not in {"select", "multi_select"}:
        prop["options"] = None
    print(f"Changed type of {name!r}: {old_type} -> {new_type}.")
    record_edit(state, "edit_type", name, {"old_type": old_type, "new_type": new_type})

def apply_edit_options(
        args: list[str],
        schema: list[NotionProperty],
        state: MigrationState,
) -> None:
    if len(args) != 2:
        print("Usage: edit_options <property_name> <opt1,opt2,opt3>")
        return 
    
    name, options_csv = args
    prop = find_property(name, schema)
    if prop is None:
        print(f"No property named {name!r}.")
        return
    
    if prop["type"] not in {"select", "multi_select"}:
        print(f"Property {name!r} is type {prop['type']!r}; options only apply to select/multi_select.")
        return
    
    new_options = [opt.strip() for opt in options_csv.split(",") if opt.strip()]
    old_options = prop["options"]
    prop["options"] = new_options
    print(f"Changed options of {name!r}: {old_options} -> {new_options}.")
    record_edit(state, "edit_options", name, {"old_options": old_options, "new_options": new_options})

def apply_edit_mapping(
        args: list[str],
        schema: list[NotionProperty],
        mapping: list[MappingEntry],
        state: MigrationState,
) -> None:
    if len(args) != 2:
        print("Usage: edit_mapping <csv_column> <property_name>")
        return   

    csv_column, prop_name = args

    # The target property must exist.
    prop = find_property(prop_name, schema) 
    if prop is None:
        print(f"No property named {prop_name!r} to map to.")
        return

    # Find the mapping entry for this csv_column, or create one.
    existing = next((m for m in mapping if m["csv_column"].lower() == csv_column.lower()), None)
    if existing is None:
        new_mapping: MappingEntry = {
            "csv_column": csv_column,
            "notion_property": prop["name"],
            "confidence": "low" # human-set mappings start at low confidence
        }   
        mapping.append(new_mapping)
        print(f"Created mapping: {csv_column!r} -> {prop['name']!r}.")
        record_edit(state, "create_mapping", csv_column, {"notion_property": prop["name"]})
    else:
        old_target = existing["notion_property"]
        existing["notion_property"] = prop["name"]
        print(f"Changed mapping of {csv_column!r}: {old_target!r} -> {prop['name']!r}.")
        record_edit(state, "edit_mapping", csv_column, {
            "old_property": old_target,
            "new_property": prop["name"],
        })

def apply_remove_mapping(
    args: list[str],
    mapping: list[MappingEntry],
    state: MigrationState,
) -> None:
    if len(args) != 1:
        print("Usage: remove_mapping <csv_column>")
        return

    csv_column = args[0]
    existing = next((m for m in mapping if m["csv_column"].lower() == csv_column.lower()), None)
    if existing is None:
        print(f"No mapping found for CSV column {csv_column!r}.")
        return

    mapping.remove(existing)
    print(f"Removed mapping: {csv_column!r} → {existing['notion_property']!r}.")
    record_edit(state, "remove_mapping", csv_column, {
        "previous_property": existing["notion_property"],
    })

# Coercion failure threshold for halting the run.
# Per-column failure rate above this triggers rejection back to the checkpoint.
# TODO: move to configuration, per-deployment tunable.
COERCION_FAILURE_THRESHOLD = 0.05  # 5%


def coercion_preview_node(state: MigrationState) -> MigrationState:
    """Simulate type coercion for each mapped property against the CSV data.
    
    Produces a coercion report. If any column's failure rate exceeds the
    threshold, routes the run back to the checkpoint with the report attached
    so the human can fix the schema with evidence in hand.
    """
    print(f"\n--- COERCION PREVIEW ---")

    # Load the CSV. Already known to be structurally valid.
    with open(state["source_csv_path"], "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    header = rows[0]
    data_rows = rows[1:]

    # Build a quick lookup of column name -> column index.
    col_index_by_name = {name: i for i, name in enumerate(header)}

    coercion_report: list[ColumnCoercionResult] = []
    any_over_threshold = False

    # For each mapped property, find its source column and run coercion.
    for mapping_entry in state["post_edit_mapping"]:
        csv_column = mapping_entry["csv_column"]
        property_name = mapping_entry["notion_property"]

        # Find the property in the post-edit schema.
        notion_property = next(
            (p for p in state["post_edit_schema"] if p["name"] == property_name),
            None,
        )
        if notion_property is None:
            # Mapping points at a property that doesn't exist (collective-validity issue).
            # We could catch this here but the architecture says checkpoint should.
            # For now, skip — sample write will surface it cleanly.
            continue

        # Find the source CSV column index.
        col_index = col_index_by_name.get(csv_column)
        if col_index is None:
            # Mapping points at a CSV column that doesn't exist in the source.
            # Same as above — skip, surface later.
            continue

        column_values = [row[col_index] for row in data_rows]
        target_type = notion_property["type"]
        options = notion_property.get("options")

        result = coerce_column(
            column_values=column_values,
            target_type=target_type,
            options=options,
            property_name=property_name,
        )
        coercion_report.append(result)

        # Check threshold on this column.
        if result["success_count"] + result["failure_count"] > 0:
            failure_rate = result["failure_count"] / (
                result["success_count"] + result["failure_count"]
            )
            if failure_rate > COERCION_FAILURE_THRESHOLD:
                any_over_threshold = True

    state["coercion_report"] = coercion_report

    if any_over_threshold:
        print("\nCoercion preview surfaced columns exceeding the failure threshold.")
        print_coercion_report(coercion_report)
        state["rejection_return_stage"] = "human_checkpoint_1"
    else:
        print("\nCoercion preview clean. Proceeding.")
        print_coercion_report(coercion_report)

    return state


def coerce_column(
    column_values: list[str],
    target_type: str,
    options: list[str] | None,
    property_name: str,
) -> ColumnCoercionResult:
    """Attempt to coerce every value in a column to the target Notion type.
    
    Returns a ColumnCoercionResult with counts and example failures.
    """
    success_count = 0
    failure_count = 0
    failures: list[CoercionFailure] = []

    for i, raw_value in enumerate(column_values, start=2):  # row 1 is header
        # Empty cells aren't failures — Notion accepts empty values for most types.
        # Title is the exception (handled below).
        if not raw_value.strip():
            if target_type == "title":
                failures.append({
                    "row_index": i,
                    "value": raw_value,
                    "reason": "title cannot be empty",
                })
                failure_count += 1
            else:
                success_count += 1
            continue

        ok, reason = try_coerce_value(raw_value, target_type, options)
        if ok:
            success_count += 1
        else:
            failure_count += 1
            # Cap failure examples at 10 to keep state and output manageable.
            if len(failures) < 10:
                failures.append({
                    "row_index": i,
                    "value": raw_value,
                    "reason": reason,
                })

    return {
        "property_name": property_name,
        "target_type": target_type,
        "success_count": success_count,
        "failure_count": failure_count,
        "failure_examples": failures,
    }


def try_coerce_value(
    value: str,
    target_type: str,
    options: list[str] | None,
) -> tuple[bool, str]:
    """Try to coerce one value to the target Notion type.
    
    Returns (success, reason_if_failed). reason is empty when success is True.
    """
    v = value.strip()

    if target_type == "title":
        # Already checked for empty above; any non-empty string is a valid title.
        return True, ""

    if target_type == "rich_text":
        # Any string works.
        return True, ""

    if target_type == "select":
        if options is None or len(options) == 0:
            return False, "select property has no defined options"
        if v in options:
            return True, ""
        return False, f"value {v!r} not in defined options {options}"

    if target_type == "multi_select":
        if options is None or len(options) == 0:
            return False, "multi_select property has no defined options"
        # Coerce: split on comma to allow multi-value cells; trim each item.
        items = [item.strip() for item in v.split(",") if item.strip()]
        if not items:
            return False, "no valid items after splitting on comma"
        bad_items = [item for item in items if item not in options]
        if bad_items:
            return False, f"items not in options: {bad_items}"
        return True, ""

    if target_type == "date":
        try:
            parse_date(v)
            return True, ""
        except (ValueError, TypeError):
            return False, "could not parse as date"

    if target_type == "number":
        try:
            float(v)
            return True, ""
        except ValueError:
            return False, "could not parse as number"

    if target_type == "checkbox":
        if v.lower() in {"true", "false", "yes", "no", "y", "n", "0", "1"}:
            return True, ""
        return False, f"value {v!r} not recognizable as boolean"

    if target_type == "email":
        # Cheap heuristic: contains @ with text on both sides.
        if "@" in v and len(v.split("@")) == 2 and all(part for part in v.split("@")):
            return True, ""
        return False, "value does not look like an email address"

    if target_type == "url":
        if v.startswith(("http://", "https://")):
            return True, ""
        return False, "url must start with http:// or https://"

    if target_type == "phone_number":
        # Cheap heuristic: contains digits, allow common separators.
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) >= 7:
            return True, ""
        return False, "value does not contain enough digits to be a phone number"

    # Unknown target type — treat as failure with explanatory reason.
    return False, f"no coercion logic defined for type {target_type!r}"


def print_coercion_report(report: list[ColumnCoercionResult]) -> None:
    """Pretty-print the coercion report for the human."""
    if not report:
        print("(no columns coerced)")
        return

    for result in report:
        total = result["success_count"] + result["failure_count"]
        if total == 0:
            print(f"\n  {result['property_name']} ({result['target_type']}): no data")
            continue
        rate = result["failure_count"] / total
        status = "OK" if rate <= COERCION_FAILURE_THRESHOLD else "OVER THRESHOLD"
        print(
            f"\n  {result['property_name']} ({result['target_type']}): "
            f"{result['success_count']}/{total} pass, "
            f"{result['failure_count']} fail "
            f"({rate*100:.1f}%) [{status}]"
        )
        for fail in result["failure_examples"][:5]:  # show up to 5 examples per column
            print(f"    row {fail['row_index']}: {fail['value']!r} — {fail['reason']}")
        if len(result["failure_examples"]) > 5:
            print(f"    ... and {len(result['failure_examples']) - 5} more")


SAMPLE_TARGET_SIZE = 5

def sample_selection_node(state: MigrationState) -> MigrationState:
    """Choose a small set of rows to write as a sample for human in-situ review.
    
    Selection is informed by earlier stage artifacts — the profile (which columns
    are messy) and the coercion report (which rows failed checks even when the
    column passed threshold) — plus row-level patterns from the raw CSV
    (longest content, most nulls).
    """
    print(f"\n--- SAMPLE SELECTION ---")

    with open(state["source_csv_path"], "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    header = rows[0]
    data_rows = rows[1:]
    col_index_by_name = {name: i for i, name in enumerate(header)}

    # Map csv_column -> set of suspicious value strings from the profile.
    suspicious_by_column: dict[str, set[str]] = {}
    for column_profile in state["profile"]:
        suspicious_by_column[column_profile["column_name"]] = {
            sv["value"] for sv in column_profile["suspicious_values"]
        }

    selected: list[SampleSelection] = []
    selected_indices: set[int] = set()

    def add_sample(row_index: int, reason: str) -> bool:
        """Add a row to the sample if not already selected. Returns True if added."""
        if row_index in selected_indices:
            return False
        if len(selected) >= SAMPLE_TARGET_SIZE:
            return False
        selected.append({"row_index": row_index, "selection_reason": reason})
        selected_indices.add(row_index)
        return True

    # 1. One clean row — first row with no suspicious cells across any column.
    for i, row in enumerate(data_rows, start=2):  # row 1 is header
        is_clean = True
        for col_name, suspicious_set in suspicious_by_column.items():
            if not suspicious_set:
                continue
            col_idx = col_index_by_name.get(col_name)
            if col_idx is None:
                continue
            if row[col_idx] in suspicious_set:
                is_clean = False
                break
        if is_clean:
            if add_sample(i, "clean row — exercises the happy path"):
                break

    # 2. Rows containing suspicious values flagged in the profile.
    for col_name, suspicious_set in suspicious_by_column.items():
        if not suspicious_set:
            continue
        col_idx = col_index_by_name.get(col_name)
        if col_idx is None:
            continue
        for i, row in enumerate(data_rows, start=2):
            if row[col_idx] in suspicious_set:
                added = add_sample(
                    i,
                    f"contains suspicious value {row[col_idx]!r} in column {col_name!r}",
                )
                if added:
                    break  # one per column is enough
        if len(selected) >= SAMPLE_TARGET_SIZE:
            break

    # 3. Rows from coercion failure examples (failures that fell under threshold).
    for result in state["coercion_report"]:
        for failure in result["failure_examples"]:
            add_sample(
                failure["row_index"],
                f"failed coercion to {result['target_type']} in property "
                f"{result['property_name']!r}: {failure['reason']}",
            )
            if len(selected) >= SAMPLE_TARGET_SIZE:
                break
        if len(selected) >= SAMPLE_TARGET_SIZE:
            break

    # 4. Row with longest combined content — tests Notion's text limits.
    if len(selected) < SAMPLE_TARGET_SIZE:
        longest_row_index = -1
        longest_length = -1
        for i, row in enumerate(data_rows, start=2):
            total = sum(len(cell) for cell in row)
            if total > longest_length:
                longest_length = total
                longest_row_index = i
        if longest_row_index >= 0:
            add_sample(
                longest_row_index,
                f"longest combined content ({longest_length} characters) — "
                "tests Notion's text limits",
            )

    # 5. Row with the most nulls — exercises empty-value behavior.
    if len(selected) < SAMPLE_TARGET_SIZE:
        most_nulls_index = -1
        most_nulls_count = -1
        for i, row in enumerate(data_rows, start=2):
            nulls = sum(1 for cell in row if not cell.strip())
            if nulls > most_nulls_count:
                most_nulls_count = nulls
                most_nulls_index = i
        # Only add this sample if the row actually has nulls; otherwise it's just
        # another clean row and we already have one.
        if most_nulls_index >= 0 and most_nulls_count > 0:
            add_sample(
                most_nulls_index,
                f"row contains {most_nulls_count} empty cell(s) — "
                "exercises empty-value handling",
            )

    state["sample_selection"] = selected
    print_sample_selection(selected, data_rows, header)
    return state


def print_sample_selection(
    selection: list[SampleSelection],
    data_rows: list[list[str]],
    header: list[str],
) -> None:
    """Show the human which rows were chosen and why."""
    if not selection:
        print("(no rows selected)")
        return

    print(f"\nSelected {len(selection)} sample row(s):")
    for item in selection:
        # data_rows is 0-indexed; row_index in state is 1-indexed (row 2 = first data)
        actual_row = data_rows[item["row_index"] - 2]
        row_preview = dict(zip(header, actual_row))
        print(f"\n  Row {item['row_index']}: {item['selection_reason']}")
        for key, value in row_preview.items():
            print(f"    {key}: {value!r}")

def sample_write_node(state: MigrationState) -> MigrationState:
    """Create the Notion database and write the selected sample rows.
    
    This is the first node that touches the live Notion API. Two distinct
    operations: database creation (one call, fatal if it fails) and page
    writes (one call per sample row, isolated failures).
    """
    print(f"\n--- SAMPLE WRITE ---")

    # 1. Create the database.
    db_id, db_error = create_notion_database(
        parent_page_id=state["target_notion_parent_id"],
        schema=state["post_edit_schema"],
        run_id=state["run_id"],
    )

    if db_id is None:
        # Schema-level failure. No database, no pages, nothing to do.
        print(f"\nDatabase creation failed: {db_error}")
        state["rejection_return_stage"] = "human_checkpoint_1"
        return state

    state["notion_database_id"] = db_id
    print(f"\nCreated Notion database: {db_id}")

    # 2. Load CSV so we can pull the actual values for each selected sample row.
    with open(state["source_csv_path"], "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    header = rows[0]
    data_rows = rows[1:]
    col_index_by_name = {name: i for i, name in enumerate(header)}

    # 3. Write each sample row as a page.
    outcomes: list[RowOutcome] = []
    for sample in state["sample_selection"]:
        row_index = sample["row_index"]
        # data_rows is 0-indexed; row_index in state is 1-indexed (row 2 = first data row)
        raw_row = data_rows[row_index - 2]

        outcome = write_notion_page(
            database_id=db_id,
            schema=state["post_edit_schema"],
            mapping=state["post_edit_mapping"],
            raw_row=raw_row,
            col_index_by_name=col_index_by_name,
            row_index=row_index,
        )
        outcomes.append(outcome)
        status = "ok" if outcome["success"] else "FAIL"
        print(f"  Row {row_index}: [{status}] {outcome.get('notion_url') or outcome.get('error_message')}")

    state["sample_row_outcomes"] = outcomes
    return state


def create_notion_database(
    parent_page_id: str,
    schema: list[NotionProperty],
    run_id: str,
) -> tuple[str | None, str | None]:
    """Create a Notion database under the given parent page.
    
    Returns (database_id, None) on success or (None, error_message) on failure.
    """
    notion_schema = build_notion_schema(schema)

    try:
        response = notion.databases.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=[{
                "type": "text",
                "text": {"content": f"Migration {run_id[:8]}"},
            }],
            properties=notion_schema,
        )
        return response["id"], None

    except APIResponseError as e:
        # Notion rejected the request — usually schema-level (invalid type, missing title, etc.)
        return None, f"Notion API error: {e.code} — {str(e)}"
    except RequestTimeoutError:
        return None, "Request timed out while creating database"
    except HTTPResponseError as e:
        return None, f"HTTP error: {e}"
    except Exception as e:
        # Catch-all so unexpected errors don't crash the pipeline.
        return None, f"Unexpected error creating database: {type(e).__name__}: {e}"


def build_notion_schema(schema: list[NotionProperty]) -> dict[str, dict]:
    """Translate our internal NotionProperty list into Notion's API schema format."""
    notion_schema: dict[str, dict] = {}
    title_assigned = False

    for prop in schema:
        prop_type = prop["type"]
        prop_name = prop["name"]

        if prop_type == "title":
            notion_schema[prop_name] = {"title": {}}
            title_assigned = True
        elif prop_type == "rich_text":
            notion_schema[prop_name] = {"rich_text": {}}
        elif prop_type == "select":
            options_list = [{"name": opt} for opt in (prop["options"] or [])]
            notion_schema[prop_name] = {"select": {"options": options_list}}
        elif prop_type == "multi_select":
            options_list = [{"name": opt} for opt in (prop["options"] or [])]
            notion_schema[prop_name] = {"multi_select": {"options": options_list}}
        elif prop_type == "date":
            notion_schema[prop_name] = {"date": {}}
        elif prop_type == "number":
            notion_schema[prop_name] = {"number": {"format": "number"}}
        elif prop_type == "checkbox":
            notion_schema[prop_name] = {"checkbox": {}}
        elif prop_type == "email":
            notion_schema[prop_name] = {"email": {}}
        elif prop_type == "url":
            notion_schema[prop_name] = {"url": {}}
        elif prop_type == "phone_number":
            notion_schema[prop_name] = {"phone_number": {}}
        else:
            # Unknown type — fall back to rich_text so the database can still be created.
            # The coercion preview should have caught this earlier.
            notion_schema[prop_name] = {"rich_text": {}}

    # Notion requires exactly one title property. If we didn't assign one,
    # promote the first property to title. (The schema review should have caught
    # this; this is defensive.)
    if not title_assigned and schema:
        first_name = schema[0]["name"]
        notion_schema[first_name] = {"title": {}}

    return notion_schema


def write_notion_page(
    database_id: str,
    schema: list[NotionProperty],
    mapping: list[MappingEntry],
    raw_row: list[str],
    col_index_by_name: dict[str, int],
    row_index: int,
) -> RowOutcome:
    """Write a single Notion page from one CSV row."""
    try:
        properties = build_page_properties(schema, mapping, raw_row, col_index_by_name)
    except Exception as e:
        return {
            "row_index": row_index,
            "success": False,
            "notion_url": None,
            "error_message": f"Failed to build page properties: {type(e).__name__}: {e}",
        }

    try:
        response = notion.pages.create(
            parent={"database_id": database_id},
            properties=properties,
        )
        return {
            "row_index": row_index,
            "success": True,
            "notion_url": response.get("url"),
            "error_message": None,
        }

    except APIResponseError as e:
        return {
            "row_index": row_index,
            "success": False,
            "notion_url": None,
            "error_message": f"Notion API error: {e.code} — {str(e)}",
        }
    except RequestTimeoutError:
        return {
            "row_index": row_index,
            "success": False,
            "notion_url": None,
            "error_message": "Request timed out while creating page",
        }
    except HTTPResponseError as e:
        return {
            "row_index": row_index,
            "success": False,
            "notion_url": None,
            "error_message": f"HTTP error: {e}",
        }
    except Exception as e:
        return {
            "row_index": row_index,
            "success": False,
            "notion_url": None,
            "error_message": f"Unexpected error: {type(e).__name__}: {e}",
        }


def build_page_properties(
    schema: list[NotionProperty],
    mapping: list[MappingEntry],
    raw_row: list[str],
    col_index_by_name: dict[str, int],
) -> dict[str, dict]:
    """Build the Notion page properties payload for one row."""
    page_properties: dict[str, dict] = {}

    # Index schema by property name for quick lookup.
    schema_by_name = {p["name"]: p for p in schema}

    for mapping_entry in mapping:
        csv_column = mapping_entry["csv_column"]
        property_name = mapping_entry["notion_property"]

        prop = schema_by_name.get(property_name)
        if prop is None:
            continue  # mapping points at nonexistent property; skip
        col_idx = col_index_by_name.get(csv_column)
        if col_idx is None:
            continue  # mapping points at nonexistent column; skip

        raw_value = raw_row[col_idx]
        prop_type = prop["type"]
        prop_options = prop.get("options")

        page_value = build_property_value(raw_value, prop_type, prop_options)
        if page_value is not None:
            page_properties[property_name] = page_value

    return page_properties


def build_property_value(
    raw_value: str,
    prop_type: str,
    options: list[str] | None,
) -> dict | None:
    """Convert a raw CSV value to Notion's property-value payload shape.
    
    Returns None for empty values on optional property types (Notion treats absence
    as empty). Returns a typed payload otherwise.
    """
    v = raw_value.strip()

    if not v and prop_type != "title":
        # Empty optional value — omit the property entirely.
        return None

    if prop_type == "title":
        return {"title": [{"text": {"content": v}}]}

    if prop_type == "rich_text":
        return {"rich_text": [{"text": {"content": v}}]}

    if prop_type == "select":
        if options is not None and v in options:
            return {"select": {"name": v}}
        return None  # not in options; coercion preview should have caught this

    if prop_type == "multi_select":
        if options is None:
            return None
        items = [item.strip() for item in v.split(",") if item.strip() in options]
        if not items:
            return None
        return {"multi_select": [{"name": item} for item in items]}

    if prop_type == "date":
        try:
            parsed = parse_date(v)
            return {"date": {"start": parsed.strftime("%Y-%m-%d")}}
        except (ValueError, TypeError):
            return None

    if prop_type == "number":
        try:
            return {"number": float(v)}
        except ValueError:
            return None

    if prop_type == "checkbox":
        if v.lower() in {"true", "yes", "y", "1"}:
            return {"checkbox": True}
        if v.lower() in {"false", "no", "n", "0"}:
            return {"checkbox": False}
        return None

    if prop_type == "email":
        return {"email": v}

    if prop_type == "url":
        return {"url": v}

    if prop_type == "phone_number":
        return {"phone_number": v}

    return None

def in_situ_checkpoint_node(state: MigrationState) -> MigrationState:
    """Second human checkpoint. Human reviews real Notion pages and decides
    whether to proceed to bulk write or rewind."""
    print(f"\n--- HUMAN CHECKPOINT 2 (IN-SITU REVIEW) ---")

    print_in_situ_context(state)
    print_checkpoint_2_help()

    while True:
        try:
            raw_input_str = input("\n> ").strip()
        except EOFError:
            print("Input stream closed. Aborting.")
            state["rejection_return_stage"] = "human_checkpoint_2"
            return state

        if not raw_input_str:
            continue

        command = raw_input_str.split()[0].lower()

        if command in {"enter", "approve"}:
            print("Approved. Proceeding to bulk write.")
            return state

        elif command == "abort":
            print("Aborting run.")
            if confirm_delete_samples():
                delete_sample_database(state)
            state["rejection_return_stage"] = "human_checkpoint_2"
            return state

        elif command == "reject_to_schema":
            print("Rewinding to checkpoint 1 (schema edit).")
            if confirm_delete_samples():
                delete_sample_database(state)
            state["rejection_return_stage"] = "human_checkpoint_1"
            return state

        elif command == "reject_to_sampling":
            print("Rewinding to sample selection.")
            if confirm_delete_samples():
                delete_sample_database(state)
            state["rejection_return_stage"] = "sample_selection"
            return state

        elif command == "help":
            print_checkpoint_2_help()

        else:
            print(f"Unknown command: {command!r}. Type 'help' for a list.")


def print_in_situ_context(state: MigrationState) -> None:
    """Show the human the sample URLs and what each one is."""
    db_id = state.get("notion_database_id")
    print(f"\nSample database: {db_id}")
    print(
        "\nReview the rendered pages in Notion. Compare what you see against\n"
        "the original CSV — confirm the data looks right, the schema renders\n"
        "the way you expect, and there are no surprises.\n"
    )
    print("Samples written:")

    # Cross-reference sample_selection (reasons) with sample_row_outcomes (URLs).
    selection_by_row = {s["row_index"]: s for s in state["sample_selection"]}
    for outcome in state["sample_row_outcomes"]:
        row_idx = outcome["row_index"]
        sel = selection_by_row.get(row_idx)
        reason = sel["selection_reason"] if sel else "(unknown reason)"
        status = "ok" if outcome["success"] else "FAIL"

        print(f"\n  Row {row_idx} [{status}]: {reason}")
        if outcome["success"] and outcome["notion_url"]:
            print(f"    → {outcome['notion_url']}")
        elif outcome["error_message"]:
            print(f"    error: {outcome['error_message']}")


def print_checkpoint_2_help() -> None:
    print("""
Available commands:
  enter / approve          — sample looks good; proceed to bulk write
  reject_to_schema         — sample reveals schema issues; rewind to checkpoint 1
  reject_to_sampling       — schema is fine but want different sample rows
  abort                    — kill the run entirely
  help                     — print this list
""")


def confirm_delete_samples() -> bool:
    """Ask the human whether to delete the sample database after rejection."""
    while True:
        answer = input(
            "\nDelete the sample database (and its pages) before rewinding? "
            "[Y/n] "
        ).strip().lower()
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def delete_sample_database(state: MigrationState) -> None:
    """Archive the sample Notion database via raw HTTP.
    
    The SDK 2.2.1 silently filters the `archived` parameter out of databases.update,
    so we bypass the SDK for this specific call and PATCH the raw endpoint directly.
    Verified working via test_archive.py.
    """
    db_id = state.get("notion_database_id")
    if not db_id:
        return

    try:
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
        response.raise_for_status()
        print(f"Archived sample database {db_id}.")
    except Exception as e:
        print(f"Couldn't archive database (will continue anyway): {type(e).__name__}: {e}")

    state["notion_database_id"] = None
    state["sample_row_outcomes"] = []

    # Bulk write thresholds — production would make these configuration, FDE-tunable per deployment.
BULK_FAILURE_RATE_THRESHOLD = 0.05         # halt if >5% of attempted rows fail
BULK_CONSECUTIVE_FAILURE_THRESHOLD = 10    # halt if this many fail in a row
BULK_MIN_ATTEMPTS_BEFORE_RATE_CHECK = 20   # rate check is meaningless on tiny samples


def bulk_write_node(state: MigrationState) -> MigrationState:
    """Write all remaining (non-sample) rows to the Notion database.
    
    Skip-and-continue is the default behavior. Halts and surfaces the run for
    human review if failure patterns suggest systematic issues — either the
    overall failure rate exceeds the threshold or N consecutive rows fail.
    """
    print(f"\n--- BULK WRITE ---")

    db_id = state["notion_database_id"]
    if db_id is None:
        # Should never happen if sample write succeeded, but defensive guard.
        print("No database ID in state. Skipping bulk write.")
        state["rejection_return_stage"] = "sample_write"
        return state

    # Load CSV.
    with open(state["source_csv_path"], "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    header = rows[0]
    data_rows = rows[1:]
    col_index_by_name = {name: i for i, name in enumerate(header)}

    # Rows already written by sample_write — skip these.
    sample_indices = {o["row_index"] for o in state["sample_row_outcomes"]}

    outcomes: list[RowOutcome] = []
    consecutive_failures = 0
    halt_reason: str | None = None

    total_rows_to_write = len(data_rows) - len(sample_indices)
    print(f"Writing {total_rows_to_write} rows ({len(data_rows)} total, {len(sample_indices)} already in sample)")

    for i, raw_row in enumerate(data_rows, start=2):  # row 1 is header
        if i in sample_indices:
            continue  # already written as part of the sample

        outcome = write_notion_page(
            database_id=db_id,
            schema=state["post_edit_schema"],
            mapping=state["post_edit_mapping"],
            raw_row=raw_row,
            col_index_by_name=col_index_by_name,
            row_index=i,
        )
        outcomes.append(outcome)

        # Progress indicator — every 10 rows for small CSVs, every 50 for large.
        progress_interval = 10 if total_rows_to_write < 100 else 50
        if len(outcomes) % progress_interval == 0:
            successes = sum(1 for o in outcomes if o["success"])
            print(f"  Progress: {len(outcomes)}/{total_rows_to_write} attempted, {successes} succeeded")

        # Track consecutive failures.
        if outcome["success"]:
            consecutive_failures = 0
        else:
            consecutive_failures += 1

        # Halt condition 1: too many consecutive failures.
        if consecutive_failures >= BULK_CONSECUTIVE_FAILURE_THRESHOLD:
            halt_reason = (
                f"halted after {consecutive_failures} consecutive failures "
                f"(threshold: {BULK_CONSECUTIVE_FAILURE_THRESHOLD}) — "
                "likely systematic issue"
            )
            break

        # Halt condition 2: overall failure rate exceeds threshold.
        # Only check after we have enough data for the rate to be meaningful.
        if len(outcomes) >= BULK_MIN_ATTEMPTS_BEFORE_RATE_CHECK:
            failure_count = sum(1 for o in outcomes if not o["success"])
            failure_rate = failure_count / len(outcomes)
            if failure_rate > BULK_FAILURE_RATE_THRESHOLD:
                halt_reason = (
                    f"halted at {len(outcomes)} attempts with "
                    f"{failure_rate*100:.1f}% failure rate "
                    f"(threshold: {BULK_FAILURE_RATE_THRESHOLD*100:.0f}%)"
                )
                break

    state["bulk_row_outcomes"] = outcomes
    print_bulk_summary(outcomes, total_rows_to_write, halt_reason)

    if halt_reason is not None:
        # Store the halt reason in state for the reconciliation report.
        # Using a dict assignment because we didn't define a typed field for it —
        # keep it simple for the learning project.
        state["bulk_write_halt_reason"] = halt_reason
        state["rejection_return_stage"] = "bulk_write"

    return state


def print_bulk_summary(
    outcomes: list[RowOutcome],
    total_to_write: int,
    halt_reason: str | None,
) -> None:
    """Print a short summary of what bulk write did."""
    if not outcomes:
        print("(no rows attempted)")
        return

    successes = sum(1 for o in outcomes if o["success"])
    failures = len(outcomes) - successes

    print(f"\nBulk write summary:")
    print(f"  Attempted: {len(outcomes)} of {total_to_write}")
    print(f"  Succeeded: {successes}")
    print(f"  Failed:    {failures}")

    if halt_reason:
        print(f"\nHALT: {halt_reason}")
        if total_to_write - len(outcomes) > 0:
            print(f"  {total_to_write - len(outcomes)} rows were not attempted.")

    if failures > 0:
        print(f"\nFirst few failures:")
        shown = 0
        for outcome in outcomes:
            if not outcome["success"]:
                print(f"  row {outcome['row_index']}: {outcome['error_message']}")
                shown += 1
                if shown >= 5:
                    break
        if failures > 5:
            print(f"  ... and {failures - 5} more")

if __name__ == "__main__":
    state = init_state(
        csv_path="Test Files/stress_test_2.csv",
        parent_id="34cb6cf3b46980c9ab00d8896467fa30",
    )
    state = structural_validation_node(state)

    if not state["structural_validation_result"]["valid"]:
        print("\nValidation failed; halting.")
        for msg in state["structural_validation_result"]["error_messages"]:
            print(f"  - {msg}")
    else:
        state = profile_node(state)
        state = schema_and_mapping_node(state)
        state = human_checkpoint_node(state)

        if state["rejection_return_stage"] is not None:
            print(f"\nRun halted at: {state['rejection_return_stage']}.")
        else:
            state = coercion_preview_node(state)

            if state["rejection_return_stage"] is not None:
                print(f"\nRun halted at: {state['rejection_return_stage']}.")
            else:
                state = sample_selection_node(state)
                state = sample_write_node(state)

                if state["rejection_return_stage"] is not None:
                    print(f"\nRun halted at: {state['rejection_return_stage']}.")
                else:
                    state = in_situ_checkpoint_node(state)

                    if state["rejection_return_stage"] is not None:
                        print(f"\nRun halted at: {state['rejection_return_stage']}.")
                    else:
                        state = bulk_write_node(state)

                        if state["rejection_return_stage"] is not None:
                            print(f"\nRun halted at: {state['rejection_return_stage']}.")
                            if state.get("bulk_write_halt_reason"):
                                print(f"  Reason: {state['bulk_write_halt_reason']}")
                        else:
                            print("\nBulk write complete. Ready for reconciliation (next node).")
                            from pprint import pp
                            print(f"\nBulk row outcomes: {len(state['bulk_row_outcomes'])} entries")
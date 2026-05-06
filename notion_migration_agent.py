from typing import TypedDict, Any
import uuid
from datetime import datetime
import csv
import os

class StructuralValidationResult(TypedDict):
    valid: bool
    error_messages: list[str]


class ColumnProfile(TypedDict):
    column_name: str
    inferred_type: str
    null_count: int
    distinct_count: int
    distinct_values_sample: list[str]
    suspicious_values: list[str]
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

if __name__ == "__main__":
    for csv_file in ["example.csv", "empty.csv", "bad_rows.csv", "not_really_csv.csv"]:
        print(f"\n{'='*60}")
        print(f"Testing: {csv_file}")
        print('='*60)
        state = init_state(
            csv_path=f"Test Files/{csv_file}",
            parent_id="34cb6cf3b46980c9ab00d8896467fa30",
        )
        state = structural_validation_node(state)
        print(f"\nValidation result:")
        print(f"  valid: {state['structural_validation_result']['valid']}")
        for msg in state['structural_validation_result']['error_messages']:
            print(f"  - {msg}")
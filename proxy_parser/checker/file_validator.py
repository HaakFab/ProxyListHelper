import os
import re
import glob
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

REPORT_FILENAMES = {
    "deck_check_report.md",
    "deck_check_summary.csv",
    "card_issues_detail.csv",
    "deck_check_report.csv",
    ".ds_store"
}

@dataclass
class FileMetadata:
    file_path: str
    file_name: str
    file_size: int
    file_type: str                  # "PLAIN_TEXT", "GDOC_SHORTCUT", "PDF_DOCUMENT", "OFFICE_DOC", "EMPTY", "UNKNOWN"
    is_valid_extension: bool
    is_valid_filename: bool
    person: str
    list_identifier: Optional[str]  # e.g., "1", "2", "Azula"
    stated_count: Optional[int]
    filename_issues: List[str] = field(default_factory=list)
    suggested_filename: Optional[str] = None

def sanitize_name(name: str) -> str:
    """Removes spaces, accents, and special characters."""
    import unicodedata
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_name = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return re.sub(r'[^a-zA-Z0-9]', '', ascii_name)

def analyze_filename(file_name: str) -> Tuple[bool, str, Optional[str], Optional[int], List[str], str]:
    """
    Analyzes filename according to updated rules:
    - Basically every filename is fine (spaces, parentheses, special characters allowed).
    - For export/identification, person name is treated as everything before the first non-letter character.
    - Divider cards and list identification use the full list name.
    - Actual card counts are used; filename count numbers are purely informational.
    Returns: (is_valid, person, list_identifier, stated_count, issues, suggested_name)
    """
    issues = []
    base_name, ext = os.path.splitext(file_name)

    # Check for double extensions e.g. .txt.gdoc or .txt.txt
    while ext.lower() in (".gdoc", ".txt") and "." in base_name:
        inner_base, inner_ext = os.path.splitext(base_name)
        if inner_ext.lower() == ".txt":
            base_name = inner_base

    # Extract person name: everything before the first non-letter character
    match = re.match(r"^([^\W\d_]+)", base_name)
    person = match.group(1) if match else base_name.strip()

    # List identifier is the remainder of base_name after person name
    remainder = base_name[len(person):].strip(" _-")
    list_identifier = remainder if remainder else None

    # Stated count if present (purely informational)
    stated_count = None
    count_match = re.search(r"[_\s(](\d{2,4})[)\s]*$", base_name)
    if count_match:
        stated_count = int(count_match.group(1))

    # Any filename is valid
    is_valid = True
    suggested_name = file_name

    return is_valid, person, list_identifier, stated_count, issues, suggested_name

def classify_file_type(file_name: str, file_size: int) -> Tuple[str, bool]:
    """Determines the file type from name and size. Returns (file_type, is_valid_extension)."""
    ext_lower = os.path.splitext(file_name)[1].lower()

    if file_size == 0:
        return "EMPTY", False
    if ext_lower == ".gdoc":
        return "GDOC_SHORTCUT", False
    if ext_lower == ".pdf":
        return "PDF_DOCUMENT", False
    if ext_lower in (".docx", ".doc", ".rtf", ".odt"):
        return "OFFICE_DOC", False
    if ext_lower == ".txt":
        return "PLAIN_TEXT", True
    return "UNKNOWN", False


def classify_upload(
    file_name: str,
    file_size: int,
    file_path: str = "",
    file_type: Optional[str] = None,
) -> FileMetadata:
    """Builds FileMetadata for a single decklist without touching the filesystem.

    The portal calls this with a submitted filename and byte count; the CLI's
    directory scan calls it per entry. Pass `file_type` to override detection
    (the portal knows a pasted list is plain text regardless of its name).
    """
    if file_type is None:
        file_type, is_valid_ext = classify_file_type(file_name, file_size)
    else:
        is_valid_ext = file_type == "PLAIN_TEXT"

    is_valid_fn, person, list_id, stated_count, fn_issues, suggested_fn = analyze_filename(file_name)

    if not is_valid_ext:
        if file_type == "GDOC_SHORTCUT":
            fn_issues.append("File is a Google Docs shortcut (.gdoc), not a plaintext .txt file")
        elif file_type == "PDF_DOCUMENT":
            fn_issues.append("File is a PDF document, not a plaintext .txt decklist")
        elif file_type == "EMPTY":
            fn_issues.append("File has 0 bytes (empty file)")
        else:
            ext = os.path.splitext(file_name)[1]
            fn_issues.append(f"Unsupported file extension '{ext}' (expected .txt)")

    return FileMetadata(
        file_path=file_path or file_name,
        file_name=file_name,
        file_size=file_size,
        file_type=file_type,
        is_valid_extension=is_valid_ext,
        is_valid_filename=is_valid_fn,
        person=person,
        list_identifier=list_id,
        stated_count=stated_count,
        filename_issues=fn_issues,
        suggested_filename=suggested_fn
    )


def cross_validate(results: List[FileMetadata]) -> List[FileMetadata]:
    """Flags people whose multiple decklists cannot be told apart by filename.

    Mutates and returns `results`. Only meaningful for the filesystem workflow:
    in the portal, lists are distinguished by account and title, not by filename.
    """
    person_files: Dict[str, List[FileMetadata]] = {}
    for f in results:
        person_files.setdefault(f.person, []).append(f)

    for person, files in person_files.items():
        if len(files) > 1:
            identifiers = set()
            for idx, f in enumerate(files):
                if not f.list_identifier:
                    f.filename_issues.append(
                        f"Person '{person}' has multiple decklists ({len(files)} files), but this file has no distinct list number or deck name (e.g. {person}1 or {person}2)"
                    )
                    f.is_valid_filename = False
                    f.suggested_filename = f"{person}{idx+1}_{f.stated_count or 100}.txt"
                elif f.list_identifier in identifiers:
                    f.filename_issues.append(
                        f"Duplicate list identifier '{f.list_identifier}' for person '{person}' (multiple files share this name)"
                    )
                    f.is_valid_filename = False
                else:
                    identifiers.add(f.list_identifier)

    return results


def discover_and_validate_files(target_dir: str) -> List[FileMetadata]:
    """Scans target_dir, excludes reports and system files, and checks file formats."""
    results = []

    if not os.path.exists(target_dir):
        return results

    for entry in sorted(os.listdir(target_dir)):
        if entry.lower() in REPORT_FILENAMES or entry.startswith("."):
            continue

        full_path = os.path.join(target_dir, entry)
        if os.path.isdir(full_path):
            continue

        results.append(classify_upload(
            file_name=entry,
            file_size=os.path.getsize(full_path),
            file_path=full_path,
        ))

    return cross_validate(results)

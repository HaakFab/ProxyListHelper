import os
import csv
import datetime
from typing import List
from rich.console import Console
from rich.table import Table
from .autofix import FileAuditResult

console = Console()

def generate_markdown_report(audits: List[FileAuditResult], output_path: str):
    """Generates a detailed, executive Markdown report with Traffic Light classification."""
    total_files = len(audits)
    green_files = [a for a in audits if a.traffic_light == "GREEN"]
    yellow_files = [a for a in audits if a.traffic_light == "YELLOW"]
    red_files = [a for a in audits if a.traffic_light == "RED"]

    total_cards = sum((a.content_report.total_cards_count if a.content_report else 0) for a in audits)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md = []
    md.append(f"# MTG Decklist Upload Audit & Scryfall Validation Report")
    md.append(f"**Generated:** {now_str}  ")
    md.append(f"**Target Directory:** `{os.path.dirname(os.path.abspath(output_path))}`  ")
    md.append(f"**Total Uploaded Files Scanned:** {total_files} | **Total Parsed Cards:** {total_cards}\n")

    # Traffic Light Summary Table
    md.append("## 🚦 Traffic Light Summary\n")
    md.append("| Status | Count | Meaning | Action Required |")
    md.append("| :--- | :--- | :--- | :--- |")
    md.append(f"| 🟢 **GREEN** | **{len(green_files)}** | Perfect format, no issues found | None (Ready for production) |")
    md.append(f"| 🟡 **YELLOW** | **{len(yellow_files)}** | Minor warnings or formatting deviations | **Can be auto-fixed** automatically |")
    md.append(f"| 🔴 **RED** | **{len(red_files)}** | Fatal errors (e.g. .gdoc shortcut, not found) | **Must be fixed by the uploader** |\n")

    # Overview Table of All Files
    md.append("## 📋 Master File Inventory\n")
    md.append("| Traffic Light | File Name | Person | Stated | Actual | Match? | Issues Detected | Can Auto-Fix? |")
    md.append("| :---: | :--- | :--- | :---: | :---: | :---: | :--- | :---: |")
    for a in audits:
        meta = a.metadata
        cnt = a.content_report
        icon = "🟢" if a.traffic_light == "GREEN" else ("🟡" if a.traffic_light == "YELLOW" else "🔴")
        stated = meta.stated_count or "-"
        actual = cnt.total_cards_count if cnt else 0
        match_str = "✓" if (meta.stated_count and meta.stated_count == actual) else ("❌" if meta.stated_count else "-")
        autofix_str = "Yes (Auto)" if a.can_auto_fix else "No (Manual)"
        summary_txt = a.status_summary.replace("|", "/")
        md.append(f"| {icon} | `{meta.file_name}` | {meta.person} | {stated} | {actual} | {match_str} | {summary_txt} | {autofix_str} |")
    md.append("\n---\n")

    # Section 1: RED (Action Required by Uploader)
    if red_files:
        md.append("## 🔴 Urgent: Files Requiring Manual / Human Fixing\n")
        md.append("> [!CAUTION]\n> The following files cannot be processed into proxies automatically without action from the uploader.\n")
        for a in red_files:
            meta = a.metadata
            cnt = a.content_report
            md.append(f"### 🔴 `{meta.file_name}` (Person: **{meta.person}**)")
            md.append(f"- **File Type**: `{meta.file_type}` | **File Size**: {meta.file_size} bytes")
            md.append("- **Blocking Problem(s)**:")
            for issue in a.manual_fix_actions:
                md.append(f"  - ❌ {issue}")
            md.append(f"- **What the Uploader Needs to Do**:")
            if meta.file_type == "GDOC_SHORTCUT":
                md.append("  - **Open the Google Doc** in your browser, click **File → Download → Plain Text (.txt)**, and upload that `.txt` file instead.")
            elif meta.file_type == "PDF_DOCUMENT":
                md.append("  - Export your decklist from Moxfield / Archidekt as a **plain text `.txt` file** instead of printing to PDF.")
            else:
                md.append(f"  - Review the errors above and re-upload a corrected `.txt` decklist.")
            md.append("")

    # Section 2: YELLOW (Auto-Fixable Warnings)
    if yellow_files:
        md.append("## 🟡 Yellow: Files with Auto-Fixable Warnings\n")
        md.append("> [!NOTE]\n> The following files contain formatting quirks or warnings that the tool can automatically resolve.\n")
        for a in yellow_files:
            meta = a.metadata
            cnt = a.content_report
            md.append(f"### 🟡 `{meta.file_name}` (Person: **{meta.person}**)")
            if meta.suggested_filename and meta.suggested_filename != meta.file_name:
                md.append(f"- **Suggested Canonical Filename**: `{meta.suggested_filename}`")
            if a.applied_changes:
                md.append("- **Applied Remediations (Exact Changes)**:")
                for chg in a.applied_changes:
                    md.append(f"  - ✓ `{chg}`")
            elif a.auto_fix_actions:
                md.append("- **Available Automatic Remediations (Run with `--apply-fixes`)**:")
                for fix in a.auto_fix_actions:
                    md.append(f"  - 🛠️ {fix}")
            if a.card_diagnostics:
                md.append("- **Card Notes**:")
                for d in a.card_diagnostics:
                    if d.severity in ("YELLOW", "INFO"):
                        md.append(f"  - *Line {d.line_number}*: **{d.card_name}** — {d.message}")
            md.append("")

    # Section 3: GREEN (Ready for Production)
    if green_files:
        md.append("## 🟢 Green: Ready for Production\n")
        md.append("The following files passed all checks, match the naming convention, and are 100% verified:\n")
        for a in green_files:
            meta = a.metadata
            cnt = a.content_report
            cards_count = cnt.total_cards_count if cnt else 0
            md.append(f"- ✅ `{meta.file_name}` ({meta.person}, **{cards_count} cards**)")
        md.append("")

    # Section 4: Exported Ready Directory
    ready_files = [a for a in audits if a.traffic_light in ("GREEN", "YELLOW") and a.can_auto_fix]
    if ready_files:
        md.append(f"## 📦 Exported to `ready/` (Ready for PDF Generation)\n")
        md.append(f"The following **{len(ready_files)} decklists** have been placed into `ready/` and are verified for printing:\n")
        for a in ready_files:
            meta = a.metadata
            cnt = a.content_report
            cards_count = cnt.total_cards_count if cnt else 0
            kind = "Clean (No Changes)" if a.traffic_light == "GREEN" else "Auto-Fixed"
            target_fn = meta.suggested_filename or meta.file_name
            if not target_fn.endswith(".txt"):
                target_fn = os.path.splitext(target_fn)[0] + ".txt"
            md.append(f"- `ready/{target_fn}` (Person: **{meta.person}**, **{cards_count} cards**) — *{kind}*")
        md.append("")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

def generate_csv_reports(audits: List[FileAuditResult], summary_csv_path: str, detail_csv_path: str):
    """Generates master summary CSV and card-level detail CSV."""
    os.makedirs(os.path.dirname(os.path.abspath(summary_csv_path)), exist_ok=True)

    # 1. Summary CSV
    with open(summary_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Traffic_Light",
            "File_Name",
            "Person",
            "List_Identifier",
            "File_Type",
            "Stated_Count",
            "Actual_Count",
            "Count_Match",
            "Missing_Set_Codes",
            "Unparseable_Lines",
            "Foil_Only_Cards",
            "Digital_Only_Cards",
            "Double_Faced_Cards",
            "Can_Auto_Fix",
            "Suggested_Filename",
            "Ready_Export_Status",
            "Ready_Filename",
            "Status_Summary"
        ])
        for a in audits:
            meta = a.metadata
            cnt = a.content_report
            actual = cnt.total_cards_count if cnt else 0
            stated = meta.stated_count or ""
            count_match = (meta.stated_count == actual) if meta.stated_count else ""
            missing_sets = cnt.missing_set_codes_count if cnt else 0
            unparseable = len(cnt.unparseable_lines) if cnt else 0
            foil_only = sum(1 for d in a.card_diagnostics if d.category == "FOIL_ONLY")
            digital_only = sum(1 for d in a.card_diagnostics if d.category == "DIGITAL_ONLY")
            double_faced = sum(1 for d in a.card_diagnostics if d.category == "DOUBLE_FACED")

            is_ready = a.traffic_light in ("GREEN", "YELLOW") and a.can_auto_fix
            ready_status = "READY (CLEAN)" if a.traffic_light == "GREEN" else ("READY (AUTO-FIXED)" if is_ready else "EXCLUDED (ACTION REQUIRED)")
            target_fn = (meta.suggested_filename or meta.file_name) if is_ready else ""
            if target_fn and not target_fn.endswith(".txt"):
                target_fn = os.path.splitext(target_fn)[0] + ".txt"

            writer.writerow([
                a.traffic_light,
                meta.file_name,
                meta.person,
                meta.list_identifier or "",
                meta.file_type,
                stated,
                actual,
                count_match,
                missing_sets,
                unparseable,
                foil_only,
                digital_only,
                double_faced,
                a.can_auto_fix,
                meta.suggested_filename or "",
                ready_status,
                target_fn,
                a.status_summary
            ])

    # 2. Card-Level Detail CSV
    with open(detail_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "File_Name",
            "Person",
            "Line_Number",
            "Card_Name",
            "Set_Code",
            "Collector_Number",
            "Severity",
            "Category",
            "Message",
            "Can_Auto_Fix",
            "Suggested_Fix",
            "Scryfall_URI"
        ])
        for a in audits:
            meta = a.metadata
            for d in a.card_diagnostics:
                writer.writerow([
                    meta.file_name,
                    meta.person,
                    d.line_number,
                    d.card_name,
                    d.set_code or "",
                    d.collector_number or "",
                    d.severity,
                    d.category,
                    d.message,
                    d.can_autofix,
                    d.suggested_fix or "",
                    d.scryfall_uri or ""
                ])

def print_terminal_summary(audits: List[FileAuditResult]):
    """Displays a rich master summary table and a detailed file-by-file changes breakdown in the console."""
    table = Table(title="MTG Decklist Audit - Traffic Light Summary", show_header=True, header_style="bold cyan")
    table.add_column("Status", justify="center")
    table.add_column("File Name", style="white")
    table.add_column("Person", style="yellow")
    table.add_column("Cards", justify="right")
    table.add_column("Auto-Fix?", justify="center")
    table.add_column("Issues Summary", style="dim")

    for a in audits:
        meta = a.metadata
        cnt = a.content_report
        actual = f"{cnt.total_cards_count}" if cnt else "0"

        if a.traffic_light == "GREEN":
            status_style = "[bold green]🟢 READY[/bold green]"
            autofix = "[green]✓[/green]"
        elif a.traffic_light == "YELLOW":
            status_style = "[bold yellow]🟡 WARNING[/bold yellow]"
            autofix = "[bold yellow]Auto-Fix[/bold yellow]"
        else:
            status_style = "[bold red]🔴 ERROR[/bold red]"
            autofix = "[bold red]Manual[/bold red]"

        table.add_row(
            status_style,
            meta.file_name,
            meta.person,
            actual,
            autofix,
            a.status_summary
        )

    console.print()
    console.print(table)
    console.print()

    # Detailed file-by-file changes breakdown
    console.print("[bold cyan]════════════════════════════════════════════════════════════════════════════════[/bold cyan]")
    console.print("[bold cyan] 📋 Detailed File-by-File Audit & Changes Breakdown[/bold cyan]")
    console.print("[bold cyan]════════════════════════════════════════════════════════════════════════════════[/bold cyan]\n")

    for a in audits:
        meta = a.metadata
        cnt = a.content_report
        actual = cnt.total_cards_count if cnt else 0

        if a.traffic_light == "GREEN":
            icon = "[bold green]🟢 READY[/bold green]"
        elif a.traffic_light == "YELLOW":
            icon = "[bold yellow]🟡 AUTO-FIXABLE[/bold yellow]"
        else:
            icon = "[bold red]🔴 ACTION REQUIRED[/bold red]"

        console.print(f"{icon} [bold white]{meta.file_name}[/bold white] (Person: [yellow]{meta.person}[/yellow] | Actual Cards: [green]{actual}[/green])")

        # Applied fixes (if already run through --apply-fixes)
        if a.applied_changes:
            console.print("  [bold green]🛠️  Applied Remediations (Exact Changes):[/bold green]")
            for chg in a.applied_changes:
                console.print(f"    [green]✓[/green] {chg}")

        # Available auto-fixes
        elif a.auto_fix_actions:
            console.print("  [bold cyan]🛠️  Auto-Fix Actions (Apply with --apply-fixes):[/bold cyan]")
            for fix in a.auto_fix_actions:
                console.print(f"    [cyan]•[/cyan] {fix}")

        # Manual blockers
        if a.manual_fix_actions:
            console.print("  [bold red]❌  Blocking Problem(s) Requiring Human Action:[/bold red]")
            for issue in a.manual_fix_actions:
                console.print(f"    [red]•[/red] {issue}")

        # Card diagnostics notes
        notes = [d for d in a.card_diagnostics if d.severity in ("YELLOW", "INFO")]
        if notes:
            console.print("  [dim]ℹ️  Card Diagnostics / Notes:[/dim]")
            for d in notes[:6]:
                console.print(f"    [dim]• Line {d.line_number}: {d.card_name} — {d.message}[/dim]")
            if len(notes) > 6:
                console.print(f"    [dim]• ... and {len(notes) - 6} more card notes (see card_issues_detail.csv)[/dim]")

        console.print()


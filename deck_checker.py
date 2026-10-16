#!/usr/bin/env python3
"""
MTG Decklist Upload Checker & Scryfall Validator CLI

Scans an upload directory (e.g. Google Drive folder Proxies_09-26), performs:
1. File format checks (.gdoc shortcuts, empty files, PDFs, .txt)
2. Filename convention checks ([Person][optional: id]_[Count].txt, multi-list validation)
3. Content & count reconciliation (header stripping, card count matching)
4. Deep Scryfall card validation (unresolved printings, digital-only, foil-only, oversized, MDFCs)
5. Traffic Light classification:
   - 🟢 GREEN: Ready for production, no errors.
   - 🟡 YELLOW: Can be auto-fixed without human intervention.
   - 🔴 RED: Requires human/user fixing.
6. Exports Markdown & CSV reports directly to the target directory.

Exit codes:
   0 - all decklists clean
   1 - at least one decklist needs auto-fixing (YELLOW)
   2 - at least one decklist needs human fixing (RED)
   3 - Scryfall unreachable, validation incomplete; no report written, because an
       unvalidated decklist would otherwise be reported as ready
"""

import os
import sys
import glob
import argparse
from typing import List
from rich.console import Console
from rich.panel import Panel

from proxy_parser.checker import (
    FileMetadata,
    discover_and_validate_files,
    parse_deck_content,
    ScryfallBatchValidator,
    ScryfallUnavailable,
    FileAuditResult,
    evaluate_traffic_light,
    apply_auto_fix_for_file,
    export_ready_deck,
    generate_markdown_report,
    generate_csv_reports,
    print_terminal_summary,
)

console = Console()

DEFAULT_GDRIVE_DIR = "/Users/fabian/Library/CloudStorage/GoogleDrive-haakfab@gmail.com/My Drive/Proxies_09-26"

def parse_args():
    parser = argparse.ArgumentParser(
        description="MTG Decklist Upload Checker & Scryfall Validator (Traffic Light System)"
    )
    default_dir = DEFAULT_GDRIVE_DIR if os.path.isdir(DEFAULT_GDRIVE_DIR) else "."
    parser.add_argument(
        "--dir", "-d",
        default=default_dir,
        help=f"Directory to scan (default: {default_dir})"
    )
    parser.add_argument(
        "--cache-dir",
        default=os.path.expanduser("~/.cache/mtg_proxy_parser/scryfall_cache"),
        help="Directory to cache Scryfall card metadata"
    )
    parser.add_argument(
        "--ready-dir",
        default=None,
        help="Output directory for all valid and auto-fixed decklists (default: <target_dir>/ready)"
    )
    parser.add_argument(
        "--no-export-ready",
        action="store_true",
        help="Skip exporting valid/fixed decklists to the ready directory (audit only)"
    )
    parser.add_argument(
        "--apply-fixes",
        action="store_true",
        help="Legacy alias to generate cleaned/fixed .txt files"
    )
    parser.add_argument(
        "--fix-dir",
        default=None,
        help="Legacy output directory for auto-fixed .txt files"
    )
    parser.add_argument(
        "--no-scryfall",
        action="store_true",
        help="Skip Scryfall API network checks (fast local syntax & filename check only)"
    )
    parser.add_argument(
        "--output-md",
        default=None,
        help="Custom path for Markdown report (default: <target_dir>/deck_check_report.md)"
    )
    parser.add_argument(
        "--output-summary-csv",
        default=None,
        help="Custom path for summary CSV (default: <target_dir>/deck_check_summary.csv)"
    )
    parser.add_argument(
        "--output-detail-csv",
        default=None,
        help="Custom path for card detail CSV (default: <target_dir>/card_issues_detail.csv)"
    )

    return parser.parse_args()

def main():
    args = parse_args()
    target_dir = os.path.abspath(args.dir)

    console.print(Panel.fit(
        f"[bold cyan]MTG Decklist Upload Checker & Scryfall Validator[/bold cyan]\n"
        f"[dim]Scanning Directory: [white]{target_dir}[/white][/dim]",
        border_style="cyan"
    ))

    if not os.path.isdir(target_dir):
        console.print(f"[bold red]Error: Target directory does not exist:[/bold red] {target_dir}")
        sys.exit(1)

    # 1. Discover and validate files
    console.print("[dim]Step 1/4: Discovering files and auditing filenames...[/dim]")
    files_meta = discover_and_validate_files(target_dir)

    if not files_meta:
        console.print("[bold yellow]No files found in target directory (excluding reports).[/bold yellow]")
        sys.exit(0)

    console.print(f"  [green]✓[/green] Found {len(files_meta)} uploaded files to evaluate.")

    # 2. Parse deck contents
    console.print("[dim]Step 2/4: Parsing deck contents & card counts...[/dim]")
    content_reports = {}
    for meta in files_meta:
        if meta.file_type in ("PLAIN_TEXT", "PDF_DOCUMENT"):
            content_reports[meta.file_path] = parse_deck_content(meta.file_path, stated_count=meta.stated_count)
        else:
            content_reports[meta.file_path] = None

    # 3. Scryfall validation
    card_diagnostics_map = {}
    if not args.no_scryfall:
        console.print("[dim]Step 3/4: Batch validating cards against Scryfall API...[/dim]")
        validator = ScryfallBatchValidator(cache_dir=args.cache_dir)
        try:
            for meta in files_meta:
                content = content_reports.get(meta.file_path)
                if content and content.cards:
                    diags = validator.validate_cards(content.cards)
                    card_diagnostics_map[meta.file_path] = diags
                else:
                    card_diagnostics_map[meta.file_path] = []
        except ScryfallUnavailable as ex:
            # Abort rather than report. Without Scryfall there are no card
            # diagnostics, and a decklist with no diagnostics looks GREEN.
            console.print(Panel(
                f"[bold red]Scryfall validation could not be completed.[/bold red]\n\n"
                f"[white]{ex}[/white]\n\n"
                f"[dim]No report was written, because an incomplete validation would "
                f"mark unchecked decklists as ready. Retry when Scryfall is reachable, "
                f"or use [white]--no-scryfall[/white] for a local syntax-only check.[/dim]",
                title="Validation Aborted",
                border_style="red"
            ))
            sys.exit(3)
    else:
        console.print("[yellow]Skipping Scryfall API checks (--no-scryfall requested).[/yellow]")
        for meta in files_meta:
            card_diagnostics_map[meta.file_path] = []

    # 4. Traffic light classification
    console.print("[dim]Step 4/4: Evaluating Traffic Light status and building reports...[/dim]")
    audits: List[FileAuditResult] = []
    for meta in files_meta:
        content = content_reports.get(meta.file_path)
        diags = card_diagnostics_map.get(meta.file_path, [])
        audit = evaluate_traffic_light(meta, content, diags)
        audits.append(audit)

    # 5. Export ALL valid decklists (clean & auto-fixed) to 'ready' directory
    ready_dir = args.ready_dir or os.path.join(target_dir, "ready")
    total_ready = 0
    ready_clean_count = 0
    ready_fixed_count = 0

    if not args.no_export_ready:
        os.makedirs(ready_dir, exist_ok=True)
        # Clear prior .txt decklists in ready_dir to prevent stale lists
        for old_file in glob.glob(os.path.join(ready_dir, "*.txt")):
            try:
                os.remove(old_file)
            except Exception:
                pass

        console.print(f"\n[bold cyan]Step 5/5: Exporting ALL valid decklists (clean + auto-fixed) to ready directory...[/bold cyan]")
        console.print(f"[dim]Target Folder: [white]{ready_dir}[/white][/dim]\n")

        for audit in audits:
            if audit.traffic_light in ("GREEN", "YELLOW") and audit.can_auto_fix:
                dest = export_ready_deck(audit, ready_dir)
                if dest:
                    dest_fn = os.path.basename(dest)
                    if audit.traffic_light == "GREEN":
                        ready_clean_count += 1
                        console.print(f"  [bold green]✓ Ready (Clean):[/bold green] [white]{audit.metadata.file_name}[/white] ➔ [cyan]ready/{dest_fn}[/cyan]")
                    else:
                        ready_fixed_count += 1
                        console.print(f"  [bold yellow]✓ Ready (Auto-Fixed):[/bold yellow] [white]{audit.metadata.file_name}[/white] ➔ [cyan]ready/{dest_fn}[/cyan]")
                        for chg in audit.applied_changes:
                            console.print(f"    [dim]• {chg}[/dim]")

        total_ready = ready_clean_count + ready_fixed_count
        console.print(f"\n[bold green]Successfully placed {total_ready} valid decklist(s) into '{os.path.basename(ready_dir)}' ({ready_clean_count} clean, {ready_fixed_count} auto-fixed).[/bold green]\n")

    # Legacy support for --fix-dir if explicitly provided
    if args.apply_fixes and args.fix_dir and os.path.abspath(args.fix_dir) != os.path.abspath(ready_dir):
        os.makedirs(args.fix_dir, exist_ok=True)
        for audit in audits:
            if audit.traffic_light == "YELLOW" and audit.can_auto_fix:
                apply_auto_fix_for_file(audit, args.fix_dir)

    # 6. Generate output reports
    md_path = args.output_md or os.path.join(target_dir, "deck_check_report.md")
    summary_csv = args.output_summary_csv or os.path.join(target_dir, "deck_check_summary.csv")
    detail_csv = args.output_detail_csv or os.path.join(target_dir, "card_issues_detail.csv")

    generate_markdown_report(audits, md_path)
    generate_csv_reports(audits, summary_csv, detail_csv)

    # 7. Print rich terminal summary table
    print_terminal_summary(audits)

    green_count = sum(1 for a in audits if a.traffic_light == "GREEN")
    yellow_count = sum(1 for a in audits if a.traffic_light == "YELLOW")
    red_count = sum(1 for a in audits if a.traffic_light == "RED")

    panel_lines = [
        f"[bold]Audit Complete![/bold]\n",
        f"🟢 Ready (Clean): [bold green]{green_count}[/bold green]",
        f"🟡 Auto-Fixable: [bold yellow]{yellow_count}[/bold yellow]",
        f"🔴 Action Required: [bold red]{red_count}[/bold red]\n",
        f"📄 [bold]Reports written to:[/bold]",
        f"  • Markdown: [cyan]{md_path}[/cyan]",
        f"  • Summary CSV: [cyan]{summary_csv}[/cyan]",
        f"  • Detail CSV: [cyan]{detail_csv}[/cyan]"
    ]
    if not args.no_export_ready:
        panel_lines.extend([
            "",
            f"📦 [bold]Ready Decklists Directory:[/bold]",
            f"  • [cyan]{ready_dir}[/cyan] ([bold green]{total_ready}[/bold green] valid decklists ready for PDF generation)"
        ])

    console.print(Panel(
        "\n".join(panel_lines),
        title="Summary Report",
        border_style="green" if red_count == 0 else ("yellow" if green_count > 0 else "red")
    ))

    if red_count > 0:
        sys.exit(2)
    elif yellow_count > 0:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()

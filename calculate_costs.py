#!/usr/bin/env python3
"""
Per-Person Cost Calculator

Combines a print-run's ``card_totals_summary.csv`` (authoritative card counts,
as actually printed -- see ``proxy_parser/pdf_builder.py:write_summary_csv``)
with the directory of submitted decklists (to count lists per person, which
the summary CSV only breaks out per-file when ``--break-by`` isn't "none")
into a per-person cost breakdown.

Pricing formula (mirrors ``portal/pricing.py``, the same arithmetic the web
portal quotes to submitters -- kept in integer cents throughout so amounts
never drift by a fraction of a cent):

    total = n_cards * price_per_card
          + n_lists * (price_per_card * 2)
          + price_per_order

Exit codes:
   0 - computed cleanly, no mismatches
   1 - computed, but some person appears on only one side (CSV vs directory)
   2 - could not read the CSV or directory
"""

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

from rich.console import Console
from rich.table import Table

from portal.pricing import Rates, compute_total_cents, format_cents_eur
from proxy_parser.deck_parser import parse_filename

console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate per-person printing cost from a card_totals_summary.csv and a directory of decklists."
    )
    parser.add_argument("csv_path", help="Path to card_totals_summary.csv (from moxfield_mpc_to_pdf.py)")
    parser.add_argument("dir_lists", help="Directory containing the submitted decklist .txt files (e.g. 'ready/')")
    parser.add_argument(
        "-x", "--price-per-card",
        type=float, default=0.13,
        help="Euro price per card (default: 0.13)"
    )
    parser.add_argument(
        "-y", "--price-per-order",
        type=float, required=True,
        help="Flat Euro fee charged once per person's order"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Where to write the per-person cost CSV (default: 'cost_per_person.csv' next to csv_path)"
    )
    return parser.parse_args()


def read_cards_per_person(csv_path: str) -> Dict[str, int]:
    """Reads the '=== Person Summary ===' block: Person, Total Cards rows."""
    if not os.path.exists(csv_path):
        console.print(f"[bold red]Error:[/bold red] CSV file not found: {csv_path}")
        sys.exit(2)

    cards_by_person: Dict[str, int] = {}
    in_person_summary = False
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            if row[0].strip() == "=== Person Summary ===":
                in_person_summary = True
                continue
            if row[0].strip().startswith("==="):
                in_person_summary = False
                continue
            if not in_person_summary or row[0].strip() == "Person":
                continue
            if len(row) < 2:
                continue
            person, count_str = row[0].strip(), row[1].strip()
            try:
                cards_by_person[person] = int(count_str)
            except ValueError:
                continue

    if not cards_by_person:
        console.print(f"[bold red]Error:[/bold red] No '=== Person Summary ===' rows found in {csv_path}")
        sys.exit(2)

    return cards_by_person


def read_lists_per_person(dir_lists: str) -> Dict[str, int]:
    """Counts decklist files per person, using the same filename convention
    (person = letters before the first digit/separator) as the rest of the
    pipeline -- see proxy_parser.deck_parser.parse_filename."""
    if not os.path.isdir(dir_lists):
        console.print(f"[bold red]Error:[/bold red] Directory not found: {dir_lists}")
        sys.exit(2)

    lists_by_person: Dict[str, int] = defaultdict(int)
    files = sorted(glob.glob(os.path.join(dir_lists, "*.txt")))
    if not files:
        console.print(f"[bold red]Error:[/bold red] No .txt decklists found in {dir_lists}")
        sys.exit(2)

    for path in files:
        _label, person, _doc_id = parse_filename(path)
        lists_by_person[person] += 1

    return dict(lists_by_person)


def build_breakdown(
    cards_by_person: Dict[str, int],
    lists_by_person: Dict[str, int],
    rates: Rates,
) -> Tuple[List[dict], List[str]]:
    """Joins both sources per person and computes cost. Returns (rows, warnings).

    A person present on only one side is a real discrepancy worth surfacing
    loudly rather than silently charging them for lists they don't have (or
    missing lists they do) -- this is a billing calculation.
    """
    all_persons = sorted(set(cards_by_person) | set(lists_by_person))
    warnings: List[str] = []
    rows: List[dict] = []

    for person in all_persons:
        cards = cards_by_person.get(person)
        lists_ = lists_by_person.get(person)

        if cards is None:
            warnings.append(f"'{person}' has {lists_} list(s) in the directory but no entry in the CSV -- excluded from cost, please check.")
            continue
        if lists_ is None:
            warnings.append(f"'{person}' has {cards} card(s) in the CSV but no matching list file(s) found -- excluded from cost, please check.")
            continue

        card_cost_cents = cards * rates.cost_per_card_cents
        list_cost_cents = lists_ * rates.cost_per_list_cents
        total_cents = compute_total_cents(cards, lists_, rates)

        rows.append({
            "person": person,
            "lists": lists_,
            "cards": cards,
            "card_cost_cents": card_cost_cents,
            "list_cost_cents": list_cost_cents,
            "order_fee_cents": rates.processing_charge_cents,
            "total_cents": total_cents,
        })

    return rows, warnings


def print_table(rows: List[dict]) -> None:
    table = Table(title="Per-Person Cost Breakdown", show_header=True, header_style="bold cyan")
    table.add_column("Person", style="white")
    table.add_column("Lists", justify="right")
    table.add_column("Cards", justify="right")
    table.add_column("Card Cost", justify="right")
    table.add_column("List Surcharge", justify="right")
    table.add_column("Order Fee", justify="right")
    table.add_column("Total", justify="right", style="bold green")

    grand_total = 0
    for r in rows:
        table.add_row(
            r["person"],
            str(r["lists"]),
            str(r["cards"]),
            format_cents_eur(r["card_cost_cents"]),
            format_cents_eur(r["list_cost_cents"]),
            format_cents_eur(r["order_fee_cents"]),
            format_cents_eur(r["total_cents"]),
        )
        grand_total += r["total_cents"]

    console.print(table)
    console.print(f"\n[bold]Grand total across {len(rows)} person(s):[/bold] [bold green]{format_cents_eur(grand_total)}[/bold green]")


def write_csv(rows: List[dict], out_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Person", "Lists", "Cards", "Card Cost (EUR)", "List Surcharge (EUR)", "Order Fee (EUR)", "Total (EUR)"])
        for r in rows:
            writer.writerow([
                r["person"],
                r["lists"],
                r["cards"],
                f"{r['card_cost_cents'] / 100:.2f}",
                f"{r['list_cost_cents'] / 100:.2f}",
                f"{r['order_fee_cents'] / 100:.2f}",
                f"{r['total_cents'] / 100:.2f}",
            ])
        grand_total = sum(r["total_cents"] for r in rows)
        writer.writerow([])
        writer.writerow(["TOTAL", "", "", "", "", "", f"{grand_total / 100:.2f}"])


def main():
    args = parse_args()

    price_per_card_cents = int(round(args.price_per_card * 100))
    price_per_order_cents = int(round(args.price_per_order * 100))
    rates = Rates(
        cost_per_card_cents=price_per_card_cents,
        cost_per_list_cents=price_per_card_cents * 2,
        processing_charge_cents=price_per_order_cents,
    )

    console.print(
        f"[cyan]Rates:[/cyan] {format_cents_eur(rates.cost_per_card_cents)}/card, "
        f"{format_cents_eur(rates.cost_per_list_cents)}/list, "
        f"{format_cents_eur(rates.processing_charge_cents)}/order\n"
    )

    cards_by_person = read_cards_per_person(args.csv_path)
    lists_by_person = read_lists_per_person(args.dir_lists)

    rows, warnings = build_breakdown(cards_by_person, lists_by_person, rates)

    if warnings:
        console.print("[bold yellow]⚠ Mismatches between the CSV and the list directory:[/bold yellow]")
        for w in warnings:
            console.print(f"  [yellow]•[/yellow] {w}")
        console.print()

    if not rows:
        console.print("[bold red]No person could be priced -- CSV and directory have no matching names.[/bold red]")
        sys.exit(2)

    print_table(rows)

    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(args.csv_path)), "cost_per_person.csv")
    write_csv(rows, out_path)
    console.print(f"\n[bold]Wrote:[/bold] {out_path}")

    sys.exit(1 if warnings else 0)


if __name__ == "__main__":
    main()

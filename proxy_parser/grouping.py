import math
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

@dataclass
class SuborderBatch:
    batch_index: int
    deck_items: List[Dict[str, Any]]
    people: List[str]
    total_cards: int
    cards_per_sheet: int
    sheets_required: int
    waste_slots: int
    description: str

def get_item_total_cards(item: Dict[str, Any]) -> int:
    """Calculates total printed cards for a deck item including its divider card(s)."""
    div_count = len(item.get("dividers", []))
    if div_count == 0 and item.get("divider"):
        div_count = 1
    return len(item.get("card_pairs", [])) + div_count

def optimize_suborders(
    deck_items: List[Dict[str, Any]],
    cards_per_sheet: int = 18,
    target_suborder_cards: int = 1440
) -> List[SuborderBatch]:
    """
    Groups deck items into optimal suborders for commercial cut & stack printing:
    - Never splits an individual decklist across suborders.
    - Groups a person's orders together (no person's lists shuffled with others).
    - Minimizes waste slots on the final sheet of each suborder.
    - Targets approximately `target_suborder_cards` (e.g. 1440 = 18 * 80 sheets).
    """
    if not deck_items:
        return []

    # 1. Cluster decks by person (preserving deterministic alphabetical order)
    person_clusters: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in deck_items:
        person = item["deck"].person if item.get("deck") else "Unknown"
        person_clusters[person].append(item)

    # 2. Sequential bin packing by person
    batches: List[SuborderBatch] = []
    current_items: List[Dict[str, Any]] = []
    current_people: List[str] = []
    current_cards = 0

    people_list = sorted(person_clusters.keys(), key=lambda p: p.lower())

    for person in people_list:
        person_items = person_clusters[person]
        person_cards = sum(get_item_total_cards(it) for it in person_items)

        # If adding this person exceeds target and current batch already has substantial cards:
        # Check if closing current batch yields less sheet waste or avoids overloading stack height
        if current_cards > 0 and (current_cards + person_cards > target_suborder_cards + (cards_per_sheet * 4)):
            # Finalize current batch
            sheets = math.ceil(current_cards / cards_per_sheet)
            waste = (sheets * cards_per_sheet) - current_cards
            batch_num = len(batches) + 1
            batches.append(SuborderBatch(
                batch_index=batch_num,
                deck_items=current_items,
                people=current_people,
                total_cards=current_cards,
                cards_per_sheet=cards_per_sheet,
                sheets_required=sheets,
                waste_slots=waste,
                description=f"Suborder {batch_num} ({current_cards} cards, {sheets} sheets @ {cards_per_sheet}/sheet, {waste} empty slots)"
            ))

            current_items = []
            current_people = []
            current_cards = 0

        # Add person to current batch
        current_items.extend(person_items)
        current_people.append(person)
        current_cards += person_cards

    # Final batch
    if current_items:
        sheets = math.ceil(current_cards / cards_per_sheet)
        waste = (sheets * cards_per_sheet) - current_cards
        batch_num = len(batches) + 1
        batches.append(SuborderBatch(
            batch_index=batch_num,
            deck_items=current_items,
            people=current_people,
            total_cards=current_cards,
            cards_per_sheet=cards_per_sheet,
            sheets_required=sheets,
            waste_slots=waste,
            description=f"Suborder {batch_num} ({current_cards} cards, {sheets} sheets @ {cards_per_sheet}/sheet, {waste} empty slots)"
        ))

    return batches

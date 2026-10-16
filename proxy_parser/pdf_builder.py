import os
import csv
from typing import Callable, List, Tuple, Dict, Any, Optional
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from .config import AppConfig

def render_pages_to_pdf(
    page_pairs: List[Tuple[str, str]],
    output_path: str,
    page_width_pt: float,
    page_height_pt: float,
    progress_callback: Optional[Callable[[int, int], None]] = None
):
    """
    Renders every card front and back onto its own individual PDF page
    at exact 69mm x 94mm card dimensions.
    Uses atomic writing (.tmp file + os.replace) so the output PDF is never
    left in an incomplete or 0-byte state while ReportLab is rendering.

    ReportLab decodes and compresses each *new* image the moment drawImage()
    is called (repeats of an already-seen filename, e.g. the shared default
    cardback, are deduped and nearly free) -- so `progress_callback`, fired
    after every card, tracks real work done rather than just page count.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    tmp_path = output_path + ".tmp"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    c = canvas.Canvas(tmp_path, pagesize=(page_width_pt, page_height_pt))

    total = len(page_pairs)
    for idx, (front_img, back_img) in enumerate(page_pairs, start=1):
        # Front Page
        c.drawImage(front_img, 0, 0, width=page_width_pt, height=page_height_pt)
        c.showPage()

        # Back Page
        c.drawImage(back_img, 0, 0, width=page_width_pt, height=page_height_pt)
        c.showPage()

        if progress_callback:
            progress_callback(idx, total)

    c.save()

    # Atomically replace final destination once fully written & flushed
    os.replace(tmp_path, output_path)

class PDFBatchBuilder:
    def __init__(self, config: AppConfig):
        self.config = config
        self.page_width_pt = config.target_card_width_mm * mm
        self.page_height_pt = config.target_card_height_mm * mm

    def build_pdfs(
        self,
        deck_items: List[Dict[str, Any]],
        output_dir: str,
        progress_callback=None
    ) -> List[Dict[str, Any]]:
        """Compiles the configured PDF split, reporting progress by rendered page-pair.

        `progress_callback(pairs_rendered, total_pairs)` fires after every
        single front/back pair across the whole run (not once per output
        file), so a single giant combined PDF (--break-by none) still shows
        real, continuously increasing progress instead of sitting at 0 until
        the entire multi-thousand-card document finishes. Divider cards count
        as a pair too since rendering one is exactly as much work as a card.
        """
        mode = self.config.break_by
        generated_summaries = []

        def sequence_len(item: Dict[str, Any]) -> int:
            return len(item["card_pairs"]) + (1 if item.get("divider") else 0)

        # Total pairs across every output file this call will produce, used
        # as the denominator for one continuous progress bar regardless of
        # mode. "count" mode never includes dividers, so it's excluded there.
        if mode == "count":
            total_cards_overall = sum(len(item["card_pairs"]) for item in deck_items)
        else:
            total_cards_overall = sum(sequence_len(item) for item in deck_items)
        rendered_so_far = 0

        def track(file_rendered: int, _file_total: int):
            if progress_callback:
                progress_callback(rendered_so_far + file_rendered, max(total_cards_overall, 1))

        def finish_file(file_sequence_len: int):
            nonlocal rendered_so_far
            rendered_so_far += file_sequence_len

        if mode == "none":
            full_sequence = []
            total_card_count = 0
            for item in deck_items:
                if item.get("divider"):
                    full_sequence.append(item["divider"])
                full_sequence.extend(item["card_pairs"])
                total_card_count += len(item["card_pairs"])

            out_path = os.path.join(output_dir, self.config.output_pdf)
            render_pages_to_pdf(full_sequence, out_path, self.page_width_pt, self.page_height_pt, progress_callback=track)
            finish_file(len(full_sequence))
            generated_summaries.append({
                "file": out_path,
                "cards": total_card_count,
                "pages": len(full_sequence) * 2,
                "label": "All Decks Combined"
            })

        elif mode == "name":
            from collections import defaultdict
            person_decks = defaultdict(list)
            for item in deck_items:
                person = item["deck"].person
                person_decks[person].append(item)

            for person, items in person_decks.items():
                person_sequence = []
                cards_count = 0
                for item in items:
                    if item.get("divider"):
                        person_sequence.append(item["divider"])
                    person_sequence.extend(item["card_pairs"])
                    cards_count += len(item["card_pairs"])

                out_path = os.path.join(output_dir, f"{person}_cards.pdf")
                render_pages_to_pdf(person_sequence, out_path, self.page_width_pt, self.page_height_pt, progress_callback=track)
                finish_file(len(person_sequence))
                generated_summaries.append({
                    "file": out_path,
                    "cards": cards_count,
                    "pages": len(person_sequence) * 2,
                    "label": f"Person: {person}"
                })

        elif mode == "document":
            for item in deck_items:
                doc_sequence = []
                if item.get("divider"):
                    doc_sequence.append(item["divider"])
                doc_sequence.extend(item["card_pairs"])

                doc_id = item["deck"].doc_id
                out_path = os.path.join(output_dir, f"{doc_id}_cards.pdf")
                render_pages_to_pdf(doc_sequence, out_path, self.page_width_pt, self.page_height_pt, progress_callback=track)
                finish_file(len(doc_sequence))
                generated_summaries.append({
                    "file": out_path,
                    "cards": len(item["card_pairs"]),
                    "pages": len(doc_sequence) * 2,
                    "label": f"Document: {doc_id}"
                })

        elif mode == "count":
            max_cards = max(1, self.config.break_count)
            all_cards = []
            for item in deck_items:
                for pair in item["card_pairs"]:
                    all_cards.append(pair)

            total_batches = (len(all_cards) + max_cards - 1) // max_cards
            for batch_num in range(total_batches):
                chunk = all_cards[batch_num * max_cards:(batch_num + 1) * max_cards]
                out_path = os.path.join(output_dir, f"cards_part{batch_num + 1}_of_{total_batches}.pdf")
                render_pages_to_pdf(chunk, out_path, self.page_width_pt, self.page_height_pt, progress_callback=track)
                finish_file(len(chunk))
                generated_summaries.append({
                    "file": out_path,
                    "cards": len(chunk),
                    "pages": len(chunk) * 2,
                    "label": f"Batch {batch_num + 1}/{total_batches}"
                })

        if progress_callback:
            progress_callback(max(total_cards_overall, 1), max(total_cards_overall, 1))

        return generated_summaries

def write_summary_csv(
    csv_path: str,
    person_counts: Dict[str, int],
    file_summaries: List[Dict[str, Any]]
):
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["=== Person Summary ==="])
        writer.writerow(["Person", "Total Cards"])
        for person, count in sorted(person_counts.items()):
            writer.writerow([person, count])

        writer.writerow([])
        writer.writerow(["=== Generated PDF Files ==="])
        writer.writerow(["File Name", "Total Cards", "Total Pages", "Description"])
        for summary in file_summaries:
            writer.writerow([
                os.path.basename(summary["file"]),
                summary["cards"],
                summary["pages"],
                summary["label"]
            ])

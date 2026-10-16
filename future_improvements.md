# Future Improvements & Architectural Roadmap

This document outlines potential future enhancements for `mtg_proxy_parser`, focusing on Scryfall API metadata utilization, super-resolution neural upscaling improvements, and print prepress enhancements.

---

## 1. Scryfall API Deep Metadata Utilization

While the current system uses Scryfall's collection API for image URLs and base card type/frame classification, the Scryfall API returns rich metadata that can automate and refine image processing:

### A. Dynamic Frame & Layout Classification
* **Frame Versions (`frame`)**:
  * Instead of solely checking `2015`, support vintage frames (`1993`, `1997`), Modern eighth-edition (`2003`), and Future Sight (`future`).
  * Each frame has distinct copyright line placements and typography.
* **Layouts (`layout`)**:
  * Handle special card layouts with tailored upscaling and cropping rules:
    * `split` / `aftermath` / `flip`: Text lines run in multiple orientations.
    * `adventure`: Dual text boxes (left adventure creature spell, right permanent spell).
    * `planar` / `scheme`: Oversized horizontal orientation requiring 3.5" x 5.0" aspect ratios instead of 2.5" x 3.5".
    * `class` / `saga`: Vertical chapter column progressions.
* **Frame Effects (`frame_effects`)**:
  * Scryfall provides an array of visual traits:
    * `extendedart` & `borderless`: Skip black-border cleanup algorithms and route immediately to mirror/replicate edge expansion.
    * `showcase`: Unique theme borders (e.g. Eldraine storybook, Kamigawa neon ink, Phyrexian).
    * `legendary`: Crowned top border requires specialized top bleed feathering.
    * `tombstone`, `miracle`, `snow`, `colorshifted`, `nyxtouched`.
* **Security Stamps (`security_stamp`)**:
  * Detect hologram stamps (`oval`, `triangle` for Universes Beyond, `acorn` for Un-sets, `heart`, `arena`).
  * Allows surgical removal or blanking of the hologram stamp for custom proxy printers without accidentally blurring rules text.
* **Border Color (`border_color`)**:
  * Scryfall explicitly specifies `border_color: "black" | "white" | "borderless" | "silver" | "gold"`.
  * Avoids heuristic edge sampling errors by directly knowing if a card has a white border (e.g., *Unlimited*, *Revised*, *7th Edition*) and handling corner feathering accordingly.

---

## 2. Super-Resolution & Image Quality Enhancements

### A. Hybrid Neural Art + Vector Typography Reconstruction
* **The "Neural Text" Problem**: Real-ESRGAN excels at artistic textures, paintings, and complex gradients, but neural super-resolution can introduce slight rounding or waviness to small 6pt font typography, collector info, and artist credits.
* **Hybrid Rendering Pipeline**:
  1. Upscale the card illustration artwork using Real-ESRGAN.
  2. Re-render the rules text, card title, and collector information digitally using vectorized MTG fonts (Beleren, Plantin, Relay) and SVG mana symbols from Scryfall.
  3. Result: Museum-grade 1200+ DPI razor-sharp text with pristine neural-enhanced artwork.

### B. Modern Model Architectures
* **HAT (Hybrid Attention Transformer) / DAT (Dual Aggregation Transformer)**:
  * Newer super-resolution architectures outperform RRDBNet on fine high-frequency patterns like MTG parchment textures and card textures.
* **Compact / Fast Models**:
  * Introduce Real-ESRGAN-Compact (`realesr-general-x4v3`) for users on older machines or CPU-only environments, delivering 4x-5x faster inference speeds with low memory footprint.

### C. Mac MPS & Pipeline Parallelism
* **Asynchronous Pipelining**:
  * Run download worker threads, MPS GPU inference, and ReportLab PDF disk writes concurrently using `asyncio` or `concurrent.futures`.
  * While Card *N+1* is downloading and Card *N-1* is writing to PDF, Card *N* is upscaling on Apple Silicon GPU, achieving 100% hardware saturation.
* **Dynamic Batching**:
  * For GPUs with 16GB+ unified memory (M-series Pro/Max/Ultra), scale patch inference batch sizes dynamically from 4 to 16 for near-instant upscaling.

---

## 3. Prepress & MPC Print Production

### A. CMYK & Color Space Management
* Card scans from Scryfall are in sRGB.
* MakePlayingCards and offset card printers typically print on CMYK presses (e.g., FOGRA39 or GRACoL profiles).
* Future option: Embed accurate color profiles or soft-proof against standard cardstock Gamuts.

### B. Custom Back Side Selection
* Support assigning different backs to different decks or persons (e.g., custom commander art, personalized back per player, or double-sided token backs).

### C. Automated Cutting Guides & Registration Marks
* For users printing proxies at home rather than MakePlayingCards, add an optional overlay for cutting registration lines and corner-rounding guides (standard 3mm radius).

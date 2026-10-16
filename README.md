# ProxyListHelper

A modern, mobile-friendly MTG decklist converter and prepress staging tool for proxy print runs (MPC - MakePlayingCards).

Hosted on [Render.com](https://render.com) (or any Python hosting platform), users can submit multiple Moxfield decklists, choose preferred card languages, configure boards (Commanders, Mainboard, Sideboard, Considering), and upload custom card backs.

The server handles automated foil-to-paper conversion, promo planeswalker stamp replacements, and interactive corner-case resolution. The admin can configure live pricing per card, list, and order, track payment statuses, and download a complete **Master Print ZIP** structured for immediate AI upscaling and PDF generation with `moxfield_mpc_to_pdf.py`.

---

## 🚀 Key Features

* **Mobile-First Responsive Interface**: Clean dark-mode glassmorphic theme designed for mobile phones and desktops.
* **Bilingual UI (English & German)**: Instant language toggle (`EN | DE`).
* **Multi-Deck Submissions**: Enter your name once, submit 1 to *N* decklists in a single order.
* **Moxfield Fetch & Preview**: Paste Moxfield URLs (with live preview) or paste raw text.
* **Preferred Card Language**: Choose deck card language (`en` default, `de`, `fr`, `es`, `it`, `ja`, etc.). Scryfall scans are fetched in the chosen language when available, falling back cleanly to English.
* **Automated Transformations**:
  * Auto-converts foil-only printings to non-foil paper versions.
  * Replaces promo planeswalker stamped cards with clean standard printings.
* **Custom Card Backs**: Upload custom card back images per deck (stored securely in DB; no lost uploads on server restarts).
* **Interactive Corner-Case Review**: Resolve 🔴 Red cards (unresolved/digital-only) before submission via action sheets.
* **Admin Dashboard & Pricing**:
  * Live pricing editor (price per card, price per list, order fee).
  * Submissions table with status toggling (`Paid` / `Ready`).
  * One-click **Summary CSV** export.
  * One-click **Master Print ZIP** export in the exact directory format required by `moxfield_mpc_to_pdf.py`.

---

## 📦 Directory Structure in Master Print ZIP

When the admin downloads the Master ZIP, it is organized for direct processing:

```text
master_print_run/
├── summary_costs.csv                 # Detailed breakdown per person and deck
└── decks/
    ├── Chris1_GlarbCEDH/
    │   ├── decklist.txt              # Contains "// Language: de" + clean card list
    │   └── back.jpg                  # Custom card back image (if uploaded)
    └── Chris2_Hydras/
        └── decklist.txt              # Uses global default cardback
```

To run prepress production on your Mac:
```bash
# 1. Unzip the export into a directory (e.g. ready/)
unzip master_print_run.zip -d ./ready

# 2. Run the upscaling & PDF compiler
python3 moxfield_mpc_to_pdf.py --input-dir ready/decks --mode upscaled --device auto
```

---

## 🌐 Deploying to Render.com (Free Tier)

This repository includes a `render.yaml` blueprint for one-click setup.

1. Create a free account on [Render.com](https://render.com).
2. Connect your GitHub account and select the **`HaakFab/ProxyListHelper`** repository.
3. In Render, select **"New Blueprint Instance"** (or create a Web Service).
4. Configure environment variables (optional overrides):
   * `ADMIN_PASSWORD`: Your secret admin password (defaults to `admin123`).
   * `PRICE_PER_CARD_CENTS`: Default `13` (0.13 €).
   * `PRICE_PER_LIST_CENTS`: Default `26` (0.26 €).
   * `ORDER_FEE_CENTS`: Default `0` (0.00 €).
   * `DATABASE_URL`: (Optional) Connect a free PostgreSQL database (e.g. from Supabase or Neon). If omitted, uses local SQLite.
5. Click **Deploy**! Render will automatically install dependencies and start the app at `https://<your-service>.onrender.com`.

---

## 💻 Running Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the dev server
python3 main.py
# or
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.  
Admin panel is available at [http://localhost:8000/admin](http://localhost:8000/admin).

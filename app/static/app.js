/**
 * ProxyListHelper Client-Side Interactions
 * Dynamic decklist adder, Moxfield auto-preview, tab switching, and image previews
 */

let deckCount = 1;

function switchTab(deckIndex, tabName) {
  const deckCard = document.getElementById(`deck-card-${deckIndex}`);
  if (!deckCard) return;

  const tabs = deckCard.querySelectorAll('.tab-btn');
  const contents = deckCard.querySelectorAll('.tab-content');
  const sourceInput = deckCard.querySelector(`input[name="source_type_${deckIndex}"]`);

  tabs.forEach(tab => {
    tab.classList.toggle('active', tab.getAttribute('data-tab') === tabName);
  });

  contents.forEach(content => {
    content.classList.toggle('active', content.getAttribute('data-tab-content') === tabName);
  });

  if (sourceInput) {
    sourceInput.value = tabName;
  }
}

function handleMoxfieldInput(deckIndex) {
  const urlInput = document.getElementById(`moxfield_url_${deckIndex}`);
  const titleInput = document.getElementById(`deck_name_${deckIndex}`);
  const statusBadge = document.getElementById(`preview_status_${deckIndex}`);
  const sideboardCheckbox = document.getElementById(`include_sideboard_${deckIndex}`);
  const consideringCheckbox = document.getElementById(`include_considering_${deckIndex}`);

  if (!urlInput || !urlInput.value.trim()) {
    if (statusBadge) statusBadge.style.display = 'none';
    return;
  }

  const url = urlInput.value.trim();
  if (!url.includes('moxfield.com/decks/')) {
    return;
  }

  if (statusBadge) {
    statusBadge.style.display = 'inline-flex';
    statusBadge.className = 'badge badge-yellow';
    statusBadge.textContent = 'Fetching Moxfield preview...';
  }

  fetch('/api/moxfield/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url: url })
  })
  .then(res => res.json())
  .then(data => {
    if (data.success) {
      if (titleInput && (!titleInput.value || titleInput.value.startsWith('Deck '))) {
        titleInput.value = data.name;
      }
      if (statusBadge) {
        statusBadge.className = 'badge badge-green';
        statusBadge.textContent = `✓ ${data.name} (${data.main_count} cards)`;
      }
      if (data.has_sideboard && sideboardCheckbox) {
        sideboardCheckbox.parentElement.style.fontWeight = '600';
      }
    } else {
      if (statusBadge) {
        statusBadge.className = 'badge badge-red';
        statusBadge.textContent = 'Moxfield preview unavailable (check URL)';
      }
    }
  })
  .catch(() => {
    if (statusBadge) {
      statusBadge.style.display = 'none';
    }
  });
}

function handleImageUpload(input, deckIndex) {
  const preview = document.getElementById(`preview_img_${deckIndex}`);
  const prompt = document.getElementById(`upload_prompt_${deckIndex}`);
  
  if (input.files && input.files[0]) {
    const file = input.files[0];
    const reader = new FileReader();
    reader.onload = function(e) {
      if (preview) {
        preview.src = e.target.result;
        preview.style.display = 'block';
      }
      if (prompt) {
        prompt.textContent = `✓ ${file.name} (${Math.round(file.size / 1024)} KB)`;
      }
    };
    reader.readAsDataURL(file);
  }
}

function addDeckForm() {
  const container = document.getElementById('decks-container');
  const countInput = document.getElementById('deck_count');
  if (!container || !countInput) return;

  const idx = deckCount;
  deckCount++;
  countInput.value = deckCount;

  const cardHtml = `
  <div class="glass-card" id="deck-card-${idx}">
    <div class="card-header-bar">
      <span class="deck-tag">Deck #${idx + 1}</span>
      <button type="button" class="btn btn-danger" onclick="removeDeckForm(${idx})">✕ Remove</button>
    </div>

    <div class="form-group">
      <label class="form-label" for="deck_name_${idx}">Deck Name</label>
      <input type="text" class="form-input" id="deck_name_${idx}" name="deck_name_${idx}" placeholder="e.g. Commander, cEDH, Standard..." value="Deck ${idx + 1}">
    </div>

    <input type="hidden" name="source_type_${idx}" value="url">

    <div class="tabs-nav">
      <button type="button" class="tab-btn active" data-tab="url" onclick="switchTab(${idx}, 'url')">Moxfield URL</button>
      <button type="button" class="tab-btn" data-tab="text" onclick="switchTab(${idx}, 'text')">Paste Text</button>
    </div>

    <div class="tab-content active" data-tab-content="url">
      <div class="form-group">
        <label class="form-label" for="moxfield_url_${idx}">Moxfield Deck URL</label>
        <input type="url" class="form-input" id="moxfield_url_${idx}" name="moxfield_url_${idx}" 
               placeholder="https://moxfield.com/decks/..." onchange="handleMoxfieldInput(${idx})">
        <div style="margin-top: 8px;">
          <span id="preview_status_${idx}" class="badge" style="display: none;"></span>
        </div>
      </div>
    </div>

    <div class="tab-content" data-tab-content="text">
      <div class="form-group">
        <label class="form-label" for="raw_text_${idx}">Decklist (Plain Text)</label>
        <textarea class="form-textarea" id="raw_text_${idx}" name="raw_text_${idx}" 
                  placeholder="1 Sol Ring (CMM) 401&#10;1 Rhystic Study (WOT) 25..."></textarea>
      </div>
    </div>

    <div class="form-group">
      <label class="form-label" for="preferred_language_${idx}">Preferred Card Language</label>
      <select class="form-select" id="preferred_language_${idx}" name="preferred_language_${idx}">
        <option value="en" selected>English</option>
        <option value="de">Deutsch (German)</option>
        <option value="fr">Français (French)</option>
        <option value="es">Español (Spanish)</option>
        <option value="it">Italiano (Italian)</option>
        <option value="ja">日本語 (Japanese)</option>
        <option value="ko">한국어 (Korean)</option>
        <option value="pt">Português (Portuguese)</option>
        <option value="ru">Русский (Russian)</option>
        <option value="zhs">简体中文 (Simplified Chinese)</option>
        <option value="zht">繁體中文 (Traditional Chinese)</option>
      </select>
      <div class="form-hint">Scryfall will download scans in this language when available, falling back to English.</div>
    </div>

    <div class="options-grid">
      <label class="checkbox-label">
        <input type="checkbox" id="include_sideboard_${idx}" name="include_sideboard_${idx}">
        Include Sideboard
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="include_considering_${idx}" name="include_considering_${idx}">
        Include Considering
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="convert_foils_${idx}" name="convert_foils_${idx}" checked>
        Convert Foil-only to Non-foil Paper
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="replace_stamps_${idx}" name="replace_stamps_${idx}" checked>
        Replace Promo 'Planeswalker Stamped' Cards
      </label>
    </div>

    <div class="form-group">
      <label class="form-label">Custom Card Back (Optional)</label>
      <label class="upload-dropzone" for="custom_back_${idx}">
        <img id="preview_img_${idx}" class="upload-preview" alt="Card back preview">
        <div id="upload_prompt_${idx}" style="font-size: 0.84rem; color: var(--text-muted);">
          📷 Click to upload custom card back (JPG or PNG)
        </div>
        <input type="file" id="custom_back_${idx}" name="custom_back_${idx}" accept="image/png, image/jpeg, image/webp" 
               style="display: none;" onchange="handleImageUpload(this, ${idx})">
      </label>
    </div>
  </div>
  `;

  const div = document.createElement('div');
  div.innerHTML = cardHtml;
  container.appendChild(div.firstElementChild);
}

function removeDeckForm(idx) {
  const deckCard = document.getElementById(`deck-card-${idx}`);
  if (deckCard) {
    deckCard.remove();
  }
}

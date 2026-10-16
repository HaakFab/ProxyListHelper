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
      if (titleInput) {
        const isUserEdited = titleInput.dataset.userEdited === 'true';
        const hasCustomVal = titleInput.value && titleInput.value.trim() && !titleInput.value.startsWith('Deck ');
        if (!isUserEdited && !hasCustomVal) {
          titleInput.value = data.name;
        }
      }
      if (statusBadge) {
        statusBadge.className = 'badge badge-green';
        statusBadge.textContent = `✓ ${data.name} (${data.main_count} cards)`;
      }
      if (data.has_sideboard && sideboardCheckbox) {
        sideboardCheckbox.parentElement.style.fontWeight = '600';
      }
      const tokenCheckbox = document.getElementById(`include_tokens_${deckIndex}`);
      if (data.has_tokens && tokenCheckbox) {
        tokenCheckbox.parentElement.style.fontWeight = '600';
      }
      const stickerCheckbox = document.getElementById(`include_stickers_${deckIndex}`);
      if (data.has_stickers && stickerCheckbox) {
        stickerCheckbox.parentElement.style.fontWeight = '600';
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

  const padIdx = String(idx + 1).padStart(3, '0');
  const cardHtml = `
  <div class="glass-card" id="deck-card-${idx}">
    <div class="card-header-bar">
      <span class="deck-tag">[${padIdx}] Deck #${idx + 1}</span>
      <button type="button" class="btn btn-danger" onclick="removeDeckForm(${idx})" style="padding: 6px 12px; font-size: 0.76rem;">[✕ REMOVE]</button>
    </div>

    <div class="form-group">
      <label class="form-label" for="deck_name_${idx}">Deck Name</label>
      <input type="text" class="form-input" id="deck_name_${idx}" name="deck_name_${idx}" placeholder="e.g. Commander, cEDH, Standard...">
    </div>

    <input type="hidden" name="source_type_${idx}" value="url">

    <div class="tabs-nav">
      <button type="button" class="tab-btn active" data-tab="url" onclick="switchTab(${idx}, 'url')">[Moxfield URL]</button>
      <button type="button" class="tab-btn" data-tab="text" onclick="switchTab(${idx}, 'text')">[Paste Text]</button>
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
        <input type="checkbox" id="include_tokens_${idx}" name="include_tokens_${idx}">
        Include Tokens
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="include_stickers_${idx}" name="include_stickers_${idx}">
        Include Stickers
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="convert_foils_${idx}" name="convert_foils_${idx}" checked>
        Convert Foil-only to Non-foil Paper
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="replace_stamps_${idx}" name="replace_stamps_${idx}" checked>
        Replace Promo 'Planeswalker Stamped' Cards
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="replace_date_stamps_${idx}" name="replace_date_stamps_${idx}" checked>
        Replace 'Date-Stamped' Prerelease Cards
      </label>
      <label class="checkbox-label">
        <input type="checkbox" id="replace_digital_${idx}" name="replace_digital_${idx}" checked>
        Replace Digital Versions (MTGO Promos, etc.) with Paper
      </label>
    </div>

    <div class="form-group">
      <label class="form-label">Custom Card Back (Optional)</label>
      <label class="upload-dropzone" for="custom_back_${idx}">
        <img id="preview_img_${idx}" class="upload-preview" alt="Card back preview">
        <div id="upload_prompt_${idx}" style="font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; color: var(--text-dark);">
          [+ UPLOAD CUSTOM CARD BACK] &bull; JPG / PNG / WEBP
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

// Track user manual edits on deck names to prevent auto-overwrite
document.addEventListener('input', (e) => {
  if (e.target && e.target.id && e.target.id.startsWith('deck_name_')) {
    e.target.dataset.userEdited = "true";
  }
});

// Form Submission & Animated Progress Modal
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('submit-form');
  if (form) {
    // Prevent accidental submission when pressing Enter in text inputs / URL inputs
    form.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag !== 'textarea') {
          e.preventDefault();
          if (e.target.id && e.target.id.startsWith('moxfield_url_')) {
            e.target.blur();
          }
          return false;
        }
      }
    });

    form.addEventListener('submit', (e) => {
      // 1. Validate submitter name
      const nameInput = document.getElementById('submitter_name');
      if (!nameInput || !nameInput.value.trim()) {
        e.preventDefault();
        if (nameInput) {
          nameInput.focus();
          nameInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        alert('Please enter your name.');
        return;
      }

      // 2. Validate that at least one deck has input
      let hasAnyDeckContent = false;
      const count = parseInt(document.getElementById('deck_count')?.value || '1', 10);
      for (let i = 0; i < count; i++) {
        const card = document.getElementById(`deck-card-${i}`);
        if (!card) continue;
        const urlInput = document.getElementById(`moxfield_url_${i}`);
        const textInput = document.getElementById(`raw_text_${i}`);
        const sourceInput = card.querySelector(`input[name="source_type_${i}"]`);
        const srcType = sourceInput ? sourceInput.value : 'url';

        if (srcType === 'url' && urlInput && urlInput.value.trim()) {
          hasAnyDeckContent = true;
          break;
        } else if (srcType === 'text' && textInput && textInput.value.trim()) {
          hasAnyDeckContent = true;
          break;
        } else if ((urlInput && urlInput.value.trim()) || (textInput && textInput.value.trim())) {
          hasAnyDeckContent = true;
          break;
        }
      }

      if (!hasAnyDeckContent) {
        e.preventDefault();
        alert('Please enter at least one Moxfield deck URL or paste deck text before converting.');
        return;
      }

      // 3. Show the Processing Progress Modal
      const modal = document.getElementById('processing-modal');
      const submitBtn = document.getElementById('submit-btn');
      if (modal) {
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
      }
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.style.opacity = '0.7';
        submitBtn.textContent = '⏳ Processing...';
      }

      // 4. Animate progress bar & steps
      startProgressAnimation();
    });
  }

  // Initialize interactive card hover tooltip
  initCardHoverTooltip();
});

function startProgressAnimation() {
  const fill = document.getElementById('progress-fill');
  const percentText = document.getElementById('progress-percent');
  const statusText = document.getElementById('progress-status-text');
  
  const step1 = document.getElementById('step-1');
  const step2 = document.getElementById('step-2');
  const step3 = document.getElementById('step-3');
  const step4 = document.getElementById('step-4');

  let currentPercent = 12;
  if (fill) fill.style.width = `${currentPercent}%`;
  if (percentText) percentText.textContent = `${currentPercent}%`;

  const timeline = [
    { at: 500, pct: 32, stepActive: step1, text: step1?.querySelector('.step-label')?.textContent },
    { at: 1200, pct: 60, stepDone: step1, stepActive: step2, text: step2?.querySelector('.step-label')?.textContent },
    { at: 2200, pct: 85, stepDone: step2, stepActive: step3, text: step3?.querySelector('.step-label')?.textContent },
    { at: 3200, pct: 95, stepDone: step3, stepActive: step4, text: step4?.querySelector('.step-label')?.textContent },
  ];

  timeline.forEach(item => {
    setTimeout(() => {
      if (fill) fill.style.width = `${item.pct}%`;
      if (percentText) percentText.textContent = `${item.pct}%`;
      if (item.text && statusText) statusText.textContent = item.text;
      if (item.stepDone) {
        item.stepDone.classList.remove('active');
        item.stepDone.classList.add('done');
      }
      if (item.stepActive) {
        item.stepActive.classList.add('active');
      }
    }, item.at);
  });
}

function initCardHoverTooltip() {
  const tooltip = document.getElementById('card-hover-tooltip');
  if (!tooltip) return;

  const tooltipReason = document.getElementById('tooltip-reason');
  const v1Box = document.getElementById('tooltip-v1-box');
  const v1Title = document.getElementById('tooltip-v1-title');
  const v1Img = document.getElementById('tooltip-v1-img');
  const v1Placeholder = document.getElementById('tooltip-v1-placeholder');
  const v1Label = document.getElementById('tooltip-v1-label');

  const separator = document.getElementById('tooltip-separator');
  const v2Box = document.getElementById('tooltip-v2-box');
  const v2Title = document.getElementById('tooltip-v2-title');
  const v2Img = document.getElementById('tooltip-v2-img');
  const v2Placeholder = document.getElementById('tooltip-v2-placeholder');
  const v2Label = document.getElementById('tooltip-v2-label');

  let activeEl = null;

  function positionTooltip(e) {
    const pad = 16;
    const tooltipWidth = tooltip.offsetWidth || 340;
    const tooltipHeight = tooltip.offsetHeight || 300;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    let x = e.clientX + pad;
    let y = e.clientY - Math.floor(tooltipHeight / 2);

    if (x + tooltipWidth > viewportWidth - pad) {
      x = e.clientX - tooltipWidth - pad;
    }
    if (x < pad) x = pad;

    if (y + tooltipHeight > viewportHeight - pad) {
      y = viewportHeight - tooltipHeight - pad;
    }
    if (y < pad) y = pad;

    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${y}px`;
  }

  document.addEventListener('mouseover', (e) => {
    const row = e.target.closest('.has-card-hover');
    if (!row) return;
    activeEl = row;

    const v1 = row.dataset.v1Img || '';
    const v2 = row.dataset.v2Img || '';
    const v1Text = row.dataset.v1Label || '';
    const v2Text = row.dataset.v2Label || '';
    const reason = row.dataset.reason || '';

    // If v2 is present and distinct from v1, show comparison mode (V1 -> V2)
    const isComparison = Boolean(v2 && (v2 !== v1 || (v2Text && v2Text !== v1Text)));

    if (tooltipReason) {
      tooltipReason.textContent = reason;
      tooltipReason.style.display = reason ? 'block' : 'none';
    }

    if (isComparison) {
      // Comparison Mode (Version 1 -> Version 2)
      if (v1Title) {
        v1Title.textContent = row.dataset.v1Title || 'Version 1 (Original)';
        v1Title.style.color = '#fbbf24';
      }
      if (separator) separator.style.display = 'block';
      if (v2Box) v2Box.style.display = 'flex';
      if (v2Title) {
        v2Title.textContent = row.dataset.v2Title || 'Version 2 (Replaced)';
        v2Title.style.color = '#34d399';
      }

      if (v1Label) v1Label.textContent = v1Text;
      if (v2Label) v2Label.textContent = v2Text;

      if (v1Img) {
        if (v1) {
          v1Img.src = v1;
          v1Img.style.display = 'block';
          if (v1Placeholder) v1Placeholder.style.display = 'none';
        } else {
          v1Img.style.display = 'none';
          if (v1Placeholder) v1Placeholder.style.display = 'block';
        }
      }

      if (v2Img) {
        if (v2) {
          v2Img.src = v2;
          v2Img.style.display = 'block';
          if (v2Placeholder) v2Placeholder.style.display = 'none';
        } else {
          v2Img.style.display = 'none';
          if (v2Placeholder) v2Placeholder.style.display = 'block';
        }
      }
    } else {
      // Single Card Preview Mode
      if (v1Title) {
        v1Title.textContent = row.dataset.v1Title || 'Card Preview';
        v1Title.style.color = '#a5b4fc';
      }
      if (separator) separator.style.display = 'none';
      if (v2Box) v2Box.style.display = 'none';

      if (v1Label) v1Label.textContent = v1Text;

      const imgToUse = v1 || v2;
      if (v1Img) {
        if (imgToUse) {
          v1Img.src = imgToUse;
          v1Img.style.display = 'block';
          if (v1Placeholder) v1Placeholder.style.display = 'none';
        } else {
          v1Img.style.display = 'none';
          if (v1Placeholder) v1Placeholder.style.display = 'block';
        }
      }
    }

    tooltip.style.display = 'block';
    positionTooltip(e);
  });

  document.addEventListener('mousemove', (e) => {
    if (activeEl && tooltip.style.display !== 'none') {
      positionTooltip(e);
    }
  });

  document.addEventListener('mouseout', (e) => {
    const row = e.target.closest('.has-card-hover');
    if (row && (!e.relatedTarget || !row.contains(e.relatedTarget))) {
      activeEl = null;
      tooltip.style.display = 'none';
    }
  });
}

// ==========================================
// Interactive Card Version Switcher Modal
// ==========================================

let currentDeckId = null;
let currentDiagIdx = null;
let currentCardName = null;
let currentActiveSet = null;
let currentActiveCn = null;
let currentVersionsList = [];
let currentFilteredVersions = [];
let currentActiveFilter = 'all';

function openVersionPicker(deckId, diagIdx, cardName, currentSet, currentCn) {
  currentDeckId = deckId;
  currentDiagIdx = diagIdx;
  currentCardName = cardName;
  currentActiveSet = (currentSet || '').toLowerCase().trim();
  currentActiveCn = String(currentCn || '').trim();
  currentActiveFilter = 'all';

  const modal = document.getElementById('version-picker-modal');
  if (!modal) return;

  const cardTitle = document.getElementById('modal-card-title');
  const currentBadge = document.getElementById('modal-current-version-label');
  const searchInput = document.getElementById('version-search-input');
  const loading = document.getElementById('modal-loading-indicator');
  const emptyState = document.getElementById('modal-empty-state');
  const container = document.getElementById('modal-versions-container');

  if (cardTitle) cardTitle.textContent = cardName;
  if (currentBadge) {
    currentBadge.textContent = currentActiveSet && currentActiveCn 
      ? `${currentActiveSet.toUpperCase()} #${currentActiveCn}`
      : 'NONE';
  }
  if (searchInput) searchInput.value = '';

  // Reset filter pills
  document.querySelectorAll('.filter-pill').forEach(pill => {
    pill.classList.toggle('active', pill.dataset.filter === 'all');
  });

  if (container) {
    container.innerHTML = '';
    container.style.display = 'grid';
  }
  if (emptyState) emptyState.style.display = 'none';
  if (loading) loading.style.display = 'block';

  modal.style.display = 'flex';
  modal.setAttribute('aria-hidden', 'false');

  // Fetch versions from Scryfall API
  fetch(`/api/card-versions?name=${encodeURIComponent(cardName)}`)
    .then(res => res.json())
    .then(data => {
      if (loading) loading.style.display = 'none';
      if (data && data.success && Array.isArray(data.versions)) {
        currentVersionsList = data.versions;
        renderVersionCards();
      } else {
        currentVersionsList = [];
        if (emptyState) {
          emptyState.textContent = (data && data.error) || 'No printings found for this card.';
          emptyState.style.display = 'block';
        }
      }
    })
    .catch(() => {
      if (loading) loading.style.display = 'none';
      currentVersionsList = [];
      if (emptyState) {
        emptyState.textContent = 'Could not load printings. Check connection to Scryfall.';
        emptyState.style.display = 'block';
      }
    });
}

function closeVersionModal() {
  const modal = document.getElementById('version-picker-modal');
  if (modal) {
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
  }
}

function setVersionFilter(filterType) {
  currentActiveFilter = filterType;
  document.querySelectorAll('.filter-pill').forEach(pill => {
    pill.classList.toggle('active', pill.dataset.filter === filterType);
  });
  renderVersionCards();
}

function filterCardVersions() {
  renderVersionCards();
}

function renderVersionCards() {
  const container = document.getElementById('modal-versions-container');
  const emptyState = document.getElementById('modal-empty-state');
  const resultsBar = document.getElementById('modal-results-bar');
  const searchInput = document.getElementById('version-search-input');
  if (!container) return;

  const query = (searchInput?.value || '').toLowerCase().trim();

  currentFilteredVersions = currentVersionsList.filter(v => {
    // Text search filter
    if (query) {
      const matchSet = (v.set || '').toLowerCase().includes(query);
      const matchCn = String(v.collector_number || '').toLowerCase().includes(query);
      const matchSetName = (v.set_name || '').toLowerCase().includes(query);
      const matchLabel = (v.version_label || '').toLowerCase().includes(query);
      const matchArtist = (v.artist || '').toLowerCase().includes(query);
      if (!matchSet && !matchCn && !matchSetName && !matchLabel && !matchArtist) {
        return false;
      }
    }

    // Category pill filter
    if (currentActiveFilter === 'paper' && v.is_digital) return false;
    if (currentActiveFilter === 'nonfoil' && (!v.finishes || !v.finishes.includes('nonfoil'))) return false;
    if (currentActiveFilter === 'nostamps' && (v.has_planeswalker_stamp || v.has_date_stamp)) return false;

    return true;
  });

  if (resultsBar) {
    resultsBar.textContent = `Showing ${currentFilteredVersions.length} of ${currentVersionsList.length} printings`;
  }

  if (currentFilteredVersions.length === 0) {
    container.innerHTML = '';
    container.style.display = 'none';
    if (emptyState) emptyState.style.display = 'block';
    return;
  }

  if (emptyState) emptyState.style.display = 'none';
  container.style.display = 'grid';

  let html = '';
  currentFilteredVersions.forEach((v, idx) => {
    const isCurrent = (v.set.toLowerCase() === currentActiveSet && String(v.collector_number) === currentActiveCn);
    
    // Badges
    let badgesHtml = '';
    if (!v.is_digital) {
      badgesHtml += '<span class="v-badge v-badge-paper">Paper</span>';
    } else {
      badgesHtml += '<span class="v-badge v-badge-digital">Digital</span>';
    }

    if (v.finishes && v.finishes.includes('nonfoil')) {
      badgesHtml += '<span class="v-badge" style="background:#E0E7FF; color:#3730A3; border-color:#6366F1;">Non-Foil</span>';
    } else if (v.is_foil_only) {
      badgesHtml += '<span class="v-badge v-badge-foil">Foil Only</span>';
    }

    if (v.has_planeswalker_stamp) {
      badgesHtml += '<span class="v-badge v-badge-stamp">PW Stamp</span>';
    }
    if (v.has_date_stamp) {
      badgesHtml += '<span class="v-badge v-badge-stamp">Date Stamp</span>';
    }
    if (v.promo_types && v.promo_types.length > 0) {
      const pNote = v.promo_types[0].replace(/_/g, ' ');
      badgesHtml += `<span class="v-badge v-badge-special">${escapeHtml(pNote)}</span>`;
    }

    const imgDisplay = v.image_url 
      ? `<img src="${v.image_url}" alt="${escapeHtml(v.version_label)}" loading="lazy">`
      : `<div style="font-family: var(--font-mono); font-size: 0.68rem; color: #888; text-align: center; padding: 10px;">No scan</div>`;

    const actionHtml = isCurrent
      ? `<div class="badge-current-chosen">[✓ CURRENT SELECTION]</div>`
      : `<button type="button" class="btn-select-version" id="btn-select-v-${idx}" onclick="replaceTheReplacement(${idx})">[⚡ REPLACE THE REPLACEMENT]</button>`;

    html += `
      <div class="version-card-tile ${isCurrent ? 'is-current' : ''}">
        <div class="version-card-img-wrap">
          ${imgDisplay}
        </div>
        <div class="version-card-title">
          <span>${v.set.toUpperCase()} #${v.collector_number}</span>
          <span style="font-size: 0.72rem; color: #666;">${v.released_year || ''}</span>
        </div>
        <div class="version-card-sub" title="${escapeHtml(v.set_name)}">${escapeHtml(v.set_name)}</div>
        <div class="version-badges-row">
          ${badgesHtml}
        </div>
        ${actionHtml}
      </div>
    `;
  });

  container.innerHTML = html;
}

function replaceTheReplacement(filteredIdx) {
  const version = currentFilteredVersions[filteredIdx];
  if (!version || !currentDeckId || currentDiagIdx === null) return;

  const btn = document.getElementById(`btn-select-v-${filteredIdx}`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ SAVING...';
  }

  fetch(`/api/deck/${currentDeckId}/switch-version`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      item_idx: currentDiagIdx,
      set: version.set,
      cn: version.collector_number,
      image_url: version.image_url,
      version_label: version.version_label
    })
  })
  .then(res => res.json())
  .then(data => {
    if (data && data.success) {
      // 1. Update Chip
      const chip = document.getElementById(`v2-chip-${currentDeckId}-${currentDiagIdx}`);
      if (chip) {
        chip.textContent = data.fixed_version_label || `${version.set.toUpperCase()} #${version.collector_number}`;
      }

      // 2. Update Red item label if present
      const redLabel = document.getElementById(`substitute-label-${currentDeckId}-${currentDiagIdx}`);
      if (redLabel) {
        redLabel.textContent = `[Replace → ${version.set.toUpperCase()} #${version.collector_number}]`;
      }

      // 3. Update hidden input
      const hiddenInput = document.getElementById(`input-v2-line-${currentDeckId}-${currentDiagIdx}`);
      if (hiddenInput && data.fixed_line) {
        hiddenInput.value = data.fixed_line;
      }

      // 4. Update Hover attributes on row
      const row = document.getElementById(`replacement-row-${currentDeckId}-${currentDiagIdx}`) ||
                  document.getElementById(`issue-row-${currentDeckId}-${currentDiagIdx}`);
      if (row) {
        if (data.fixed_image_url) {
          row.dataset.v2Img = data.fixed_image_url;
        }
        row.dataset.v2Label = data.fixed_version_label || `${version.set.toUpperCase()} #${version.collector_number}`;
        row.dataset.reason = data.message || `Replaced with ${version.set.toUpperCase()} #${version.collector_number}`;

        // Flash row briefly with green outline
        row.style.outline = '3px solid var(--accent-acid)';
        row.style.transition = 'outline 0.3s ease';
        setTimeout(() => {
          row.style.outline = 'none';
        }, 1500);
      }

      // 5. Close modal
      closeVersionModal();

      // 6. Show toast
      showReplacementToast(`${version.set.toUpperCase()} #${version.collector_number}`);
    } else {
      alert((data && data.detail) || 'Failed to update replacement version.');
      if (btn) {
        btn.disabled = false;
        btn.textContent = '[⚡ REPLACE THE REPLACEMENT]';
      }
    }
  })
  .catch(() => {
    alert('Network error while saving replacement version.');
    if (btn) {
      btn.disabled = false;
      btn.textContent = '[⚡ REPLACE THE REPLACEMENT]';
    }
  });
}

function showReplacementToast(label) {
  let toast = document.getElementById('replacement-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'replacement-toast';
    toast.className = 'replacement-flash-toast';
    document.body.appendChild(toast);
  }

  toast.innerHTML = `<span>✓</span> <span>Replacement updated to <strong>${escapeHtml(label)}</strong>!</span>`;
  toast.style.display = 'flex';

  setTimeout(() => {
    if (toast) toast.style.display = 'none';
  }, 3500);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Global modal dismiss listeners
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeVersionModal();
  }
});

document.addEventListener('click', (e) => {
  const modal = document.getElementById('version-picker-modal');
  if (modal && e.target === modal) {
    closeVersionModal();
  }
});

// Bind to window for global access
window.openVersionPicker = openVersionPicker;
window.closeVersionModal = closeVersionModal;
window.setVersionFilter = setVersionFilter;
window.filterCardVersions = filterCardVersions;
window.replaceTheReplacement = replaceTheReplacement;

// ==========================================
// Meme-Tier Ultra Dark Void Mode
// ==========================================

function toggleDarkMode() {
  const isVoid = document.documentElement.classList.toggle('dark-void');
  try {
    localStorage.setItem('theme', isVoid ? 'void' : 'paper');
  } catch (e) {}
  updateDarkModeButton();
}

function updateDarkModeButton() {
  const tag = document.getElementById('darkmode-state-tag');
  const btn = document.getElementById('darkmode-toggle');
  const isVoid = document.documentElement.classList.contains('dark-void');
  if (tag) {
    tag.textContent = isVoid ? '[⚡ VOID: MAX DARK]' : '[🌑 VOID]';
  }
  if (btn) {
    btn.setAttribute('aria-pressed', isVoid ? 'true' : 'false');
    btn.title = isVoid 
      ? 'Extreme Void Dark Mode Active. Click to return to Paper mode.'
      : 'Activate Extreme Pitch-Black Void Dark Mode.';
  }
}

document.addEventListener('DOMContentLoaded', updateDarkModeButton);
window.toggleDarkMode = toggleDarkMode;
window.updateDarkModeButton = updateDarkModeButton;





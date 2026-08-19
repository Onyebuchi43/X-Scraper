/**
 * app.js — Scweet Dashboard Frontend Logic
 */

// ══ State ═════════════════════════════════════════════════════════════════════
let accounts = [];
let currentJobId = null;
let currentCampaignId = null;
let jobPollInterval = null;
let campaignPollInterval = null;
let currentTab = 'accounts';

// ══ Init ══════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  loadAccounts();
  loadCSVFiles();
  loadJobs();

  // Template char counter
  const tmpl = document.getElementById('c-post-template');
  if (tmpl) {
    tmpl.addEventListener('input', () => {
      const cnt = document.getElementById('template-counter');
      if (cnt) cnt.textContent = `${tmpl.value.length} / 240`;
    });
  }

  // Drag-over highlight for CSV upload
  const area = document.getElementById('csv-upload-area');
  if (area) {
    area.addEventListener('dragover', e => { e.preventDefault(); area.classList.add('drag-over'); });
    area.addEventListener('dragleave', () => area.classList.remove('drag-over'));
    area.addEventListener('drop', e => area.classList.remove('drag-over'));
  }

  renderCountryChips('f');
  renderCountryChips('c');
  renderCountryChips('edit-c');
  autoConnectActiveCampaign();
  loadScraperJobsTable();

  // Global Escape key: close any open modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const editModal = document.getElementById('editCampaignModal');
      if (editModal && editModal.style.display !== 'none') {
        closeEditCampaignModal();
      }
      // Close any other visible modal-backdrop
      document.querySelectorAll('[class*="modal"]').forEach(m => {
        if (m.style.display === 'flex' || m.style.display === 'block') {
          m.style.display = 'none';
        }
      });
    }
  });

  // Click outside editCampaignModal backdrop to close it
  const editModal = document.getElementById('editCampaignModal');
  if (editModal) {
    editModal.addEventListener('click', (e) => {
      if (e.target === editModal) closeEditCampaignModal();
    });
  }
});

const TAB_TITLES = {
  accounts: 'Account Manager',
  creator: 'Account Creator & Bulk Tools',
  proxy: 'Proxy & BetaSocks Settings',
  scraper: 'Twitter Scraper',
  campaign: 'Post Campaign',
  vps: 'VPS & Production Server Manager',
};

function switchTab(tab, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const target = document.getElementById('tab-' + tab);
  if (target) target.classList.add('active');
  if (el) el.classList.add('active');
  document.getElementById('tabTitle').textContent = TAB_TITLES[tab] || tab;
  currentTab = tab;

  if (tab === 'accounts') loadAccounts();
  if (tab === 'creator') loadAccounts();
  if (tab === 'proxy') loadProxySettings();
  if (tab === 'scraper') { loadAccountSelects(); loadCSVFiles(); loadScraperJobsTable(); }
  if (tab === 'campaign') { loadAccountSelects(); loadCampaignDbPanel(); loadVpsStatus(); }
  if (tab === 'vps') { loadVpsStatus(); }
}

function switchSubTab(parent, sub, el) {
  document.querySelectorAll(`#tab-${parent} .sub-panel`).forEach(p => p.classList.remove('active'));
  document.querySelectorAll(`#tab-${parent} .sub-tab`).forEach(b => b.classList.remove('active'));
  document.getElementById(`${parent}-${sub}`).classList.add('active');
  if (el) el.classList.add('active');
}

function refreshCurrentTab() { switchTab(currentTab, null); }

// ══ Accounts ══════════════════════════════════════════════════════════════════
async function loadAccounts() {
  const data = await api('/api/accounts');
  accounts = data;
  renderAccounts(data);
  document.getElementById('accounts-count').textContent = data.length;
  loadAccountSelects();
}

function renderAccounts(list) {
  const tbody = document.getElementById('accounts-tbody');
  if (!list.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No accounts yet — add one above</td></tr>';
    return;
  }
  tbody.innerHTML = list.map(a => {
    let labelHtml = '—';
    if (a.label) {
      let linkUrl = a.label;
      if (!linkUrl.startsWith('http://') && !linkUrl.startsWith('https://')) {
        linkUrl = `https://x.com/${linkUrl.replace(/^@/, '')}`;
      }
      labelHtml = `<a href="${esc(linkUrl)}" target="_blank" rel="noopener noreferrer" style="color:var(--accent-color, #4f46e5);font-weight:600;text-decoration:underline;">${esc(linkUrl)}</a>`;
    }
    return `
    <tr>
      <td>${a.id}</td>
      <td>${labelHtml}</td>
      <td><code>${esc(a.token_preview)}</code></td>
      <td>${a.proxy ? `<code>${esc(a.proxy)}</code>` : '—'}</td>
      <td>${fmtDate(a.created_at)}</td>
      <td>
        <button class="btn btn-sm btn-ghost" onclick="openEditAccountModal(${a.id})" style="margin-right:4px;">✏️ Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteAccount(${a.id})">✕ Remove</button>
      </td>
    </tr>
  `;
  }).join('');
}

async function addAccount() {
  const auth_token = val('inp-auth-token');
  const ct0 = val('inp-ct0');
  if (!auth_token || !ct0) { toast('auth_token and ct0 are required', 'error'); return; }
  const res = await api('/api/accounts', {
    method: 'POST',
    body: JSON.stringify({
      auth_token,
      ct0,
      proxy: val('inp-proxy'),
      label: val('inp-label'),
    }),
  });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('Account added!', 'success');
  ['inp-auth-token', 'inp-ct0', 'inp-proxy', 'inp-label'].forEach(id => set(id, ''));
  loadAccounts();
}

async function openEditAccountModal(id) {
  const acc = await api(`/api/accounts/${id}`);
  if (!acc || acc.error) {
    toast(acc.error || 'Failed to fetch account details', 'error');
    return;
  }
  set('edit-acc-id', acc.id);
  set('edit-acc-label', acc.label || '');
  set('edit-acc-auth-token', acc.auth_token || '');
  set('edit-acc-ct0', acc.ct0 || '');
  set('edit-acc-proxy', acc.proxy || '');
  
  const modal = document.getElementById('editAccountModal');
  if (modal) modal.style.display = 'block';
}

function closeEditAccountModal() {
  const modal = document.getElementById('editAccountModal');
  if (modal) modal.style.display = 'none';
}

async function saveAccountEdit() {
  const id = val('edit-acc-id');
  const auth_token = val('edit-acc-auth-token');
  const ct0 = val('edit-acc-ct0');
  const proxy = val('edit-acc-proxy');
  const label = val('edit-acc-label');

  if (!auth_token || !ct0) {
    toast('auth_token and ct0 are required', 'error');
    return;
  }

  const res = await api(`/api/accounts/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ auth_token, ct0, proxy, label }),
  });

  if (res.error) {
    toast(res.error, 'error');
    return;
  }

  toast('Account updated successfully!', 'success');
  closeEditAccountModal();
  loadAccounts();
}

async function deleteAccount(id) {
  if (!confirm('Remove this account?')) return;
  await api(`/api/accounts/${id}`, { method: 'DELETE' });
  toast('Account removed', 'info');
  loadAccounts();
}

function loadAccountSelects() {
  const selectors = ['f-accounts', 's-accounts', 'p-accounts', 'c-accounts'];
  selectors.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const prevSelected = Array.from(el.selectedOptions).map(o => o.value);
    el.innerHTML = accounts.map(a =>
      `<option value="${a.id}" ${prevSelected.includes(String(a.id)) ? 'selected' : ''}>
        ${esc(a.label || `Account #${a.id}`)} (${esc(a.token_preview)})
      </option>`
    ).join('');

    if (!el.dataset.hasListener) {
      el.addEventListener('change', () => updateAccountSelectBadge(id));
      el.dataset.hasListener = 'true';
    }
    updateAccountSelectBadge(id);
  });
}

let campaignSelectedCountries = {
  f: [],
  c: [],
  'edit-c': [],
};

function addCountryChip(prefix) {
  if (!campaignSelectedCountries[prefix]) {
    campaignSelectedCountries[prefix] = [];
  }
  const sel = document.getElementById(`${prefix}-country`);
  if (!sel) return;
  const val = sel.value;
  if (!val) return;

  if (!campaignSelectedCountries[prefix].includes(val)) {
    campaignSelectedCountries[prefix].push(val);
    renderCountryChips(prefix);
  }
  sel.value = '';
}

function addCustomCountryChip(prefix) {
  if (!campaignSelectedCountries[prefix]) {
    campaignSelectedCountries[prefix] = [];
  }
  const input = document.getElementById(`${prefix}-custom-country`);
  if (!input) return;
  const raw = input.value.trim();
  if (!raw) return;

  // Support single or comma-separated countries
  const countries = raw.split(',').map(s => s.trim()).filter(Boolean);
  for (const c of countries) {
    if (!campaignSelectedCountries[prefix].includes(c)) {
      campaignSelectedCountries[prefix].push(c);
    }
  }
  input.value = '';
  renderCountryChips(prefix);
}

function removeCountryChip(prefix, country) {
  campaignSelectedCountries[prefix] = (campaignSelectedCountries[prefix] || []).filter(c => c !== country);
  renderCountryChips(prefix);
}

function renderCountryChips(prefix) {
  const container = document.getElementById(`${prefix}-country-tags`);
  if (!container) return;
  const list = campaignSelectedCountries[prefix] || [];
  if (list.length === 0) {
    container.innerHTML = '<span style="font-size:12px;color:var(--text-3);font-style:italic;">🌐 All Countries / Worldwide (No Filter)</span>';
    return;
  }
  container.innerHTML = list.map(c => `
    <span class="country-chip">
      <span>${esc(c)}</span>
      <span class="country-chip-remove" onclick="removeCountryChip('${prefix}', '${esc(c)}')">✕</span>
    </span>
  `).join('');
}

function togglePostingMode(mode) {
  const step1Card = document.getElementById('step-1-card-container');
  const normalGroup = document.getElementById('group-normal-image');
  const listFields = document.querySelectorAll('.group-list-field');
  if (step1Card) {
    step1Card.style.display = (mode === 'list_card' || mode === 'normal_card') ? 'block' : 'none';
  }
  if (normalGroup) {
    normalGroup.style.display = (mode === 'normal_custom') ? 'block' : 'none';
  }
  listFields.forEach(el => {
    el.style.display = mode.startsWith('list') ? 'block' : 'none';
  });
}

function toggleEditPostingMode(mode) {
  const editListFields = document.querySelectorAll('.edit-c-list-field');
  editListFields.forEach(el => {
    el.style.display = mode.startsWith('list') ? 'block' : 'none';
  });
}

function toggleTargetType(val) {
  const isCsv = (val === 'csv_list');
  const targetGroup   = document.getElementById('group-target-input');
  const csvGroup      = document.getElementById('group-c-csv-upload');
  const rangeGroup    = document.getElementById('group-c-follower-range');
  const countryGroup  = document.getElementById('group-c-country-filter');
  const lbl           = document.getElementById('lbl-target-input');
  const inp           = document.getElementById('c-source-profiles');
  const hint          = document.getElementById('hint-target-input');

  if (targetGroup)  targetGroup.style.display  = isCsv ? 'none' : 'block';
  if (rangeGroup)   rangeGroup.style.display   = isCsv ? 'none' : 'block';
  if (countryGroup) countryGroup.style.display = isCsv ? 'none' : 'block';
  if (csvGroup)     csvGroup.style.display     = isCsv ? 'block' : 'none';

  if (!isCsv) {
    if (val === 'tweet_commenters') {
      if (lbl)  lbl.innerHTML = 'Tweet URL or Tweet ID <span class="badge-required">required</span>';
      if (inp)  inp.placeholder = 'https://x.com/elonmusk/status/1820000000000000000';
      if (hint) hint.textContent = 'Paste Tweet URL or ID. Streamingly scrapes users who commented on this tweet and tags them.';
    } else if (val === 'target_tweets_commenters') {
      if (lbl)  lbl.innerHTML = 'Target Profile(s) to Scrape Comments From <span class="badge-required">required</span>';
      if (inp)  inp.placeholder = 'elonmusk, OpenAI, nasa';
      if (hint) hint.textContent = 'Comma-separated usernames. Scrapes recent top tweets posted by target profiles and extracts comments.';
    } else {
      if (lbl)  lbl.innerHTML = 'Source Profiles to Scrape From <span class="badge-required">required</span>';
      if (inp)  inp.placeholder = 'elonmusk, OpenAI, nasa';
      if (hint) hint.textContent = 'Comma-separated usernames. Streamingly scrapes followers from these profiles without saving CSV files on disk.';
    }
  }
}

function toggleEditTargetType(val) {
  const isCsv = (val === 'csv_list');
  const editTargetGroup   = document.getElementById('group-edit-target-input');
  const editCsvGroup      = document.getElementById('group-edit-c-csv-upload');
  const editRangeGroup    = document.getElementById('group-edit-c-follower-range');
  const editCountryGroup  = document.getElementById('group-edit-c-country-filter');
  const lbl               = document.getElementById('lbl-edit-target-input');
  const inp               = document.getElementById('edit-c-source-profiles');
  const hint              = document.getElementById('hint-edit-target-input');

  if (editTargetGroup)  editTargetGroup.style.display  = isCsv ? 'none' : 'block';
  if (editRangeGroup)   editRangeGroup.style.display   = isCsv ? 'none' : 'block';
  if (editCountryGroup) editCountryGroup.style.display = isCsv ? 'none' : 'block';
  if (editCsvGroup)     editCsvGroup.style.display     = isCsv ? 'block' : 'none';

  if (!isCsv) {
    if (val === 'tweet_commenters') {
      if (lbl)  lbl.textContent = 'Tweet URL or Tweet ID';
      if (inp)  inp.placeholder = 'https://x.com/elonmusk/status/1820000000000000000';
      if (hint) hint.textContent = 'Paste Tweet URL or ID. Streamingly scrapes users who commented on this tweet and tags them.';
    } else if (val === 'target_tweets_commenters') {
      if (lbl)  lbl.textContent = 'Target Profile(s) to Scrape Comments From';
      if (inp)  inp.placeholder = 'elonmusk, OpenAI, nasa';
      if (hint) hint.textContent = 'Comma-separated usernames. Scrapes recent top tweets posted by target profiles and extracts comments.';
    } else {
      if (lbl)  lbl.textContent = 'Source Profile(s) to Scrape From';
      if (inp)  inp.placeholder = 'elonmusk, OpenAI, nasa';
      if (hint) hint.textContent = 'Comma-separated usernames. Streamingly scrapes followers from these profiles.';
    }
  }
}

let campaignCsvHandles = { 'c': [], 'edit-c': [] };

function parseCSVHandles(text) {
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  if (!lines.length) return [];

  // Detect delimiter
  const firstLine = lines[0];
  let delimiter = ',';
  if (firstLine.includes('\t')) delimiter = '\t';
  else if (firstLine.includes(';') && !firstLine.includes(',')) delimiter = ';';

  // Extract headers
  const headers = firstLine.split(delimiter).map(h => h.replace(/^["']|["']$/g, '').trim().toLowerCase());

  // Detect column: compatible with Scweet scraper outputs (username / user_screen_name)
  let targetColIdx = -1;
  const candidateHeaders = ['username', 'user_screen_name', 'screen_name', 'handle', 'user', 'screenname', 'user_name'];
  for (const ch of candidateHeaders) {
    const idx = headers.indexOf(ch);
    if (idx !== -1) {
      targetColIdx = idx;
      break;
    }
  }

  // If CSV has multiple columns and none of them is a recognized handle column, reject
  if (targetColIdx === -1 && headers.length > 1) {
    return [];
  }

  const handles = [];
  const startIdx = (targetColIdx !== -1) ? 1 : 0;
  const colToUse = (targetColIdx !== -1) ? targetColIdx : 0;

  for (let i = startIdx; i < lines.length; i++) {
    const cols = lines[i].split(delimiter).map(c => c.replace(/^["']|["']$/g, '').trim());
    if (cols[colToUse]) {
      let raw = cols[colToUse].replace(/^@+/, '').trim();
      if (raw.includes('twitter.com/') || raw.includes('x.com/')) {
        raw = raw.split('/').pop().split('?')[0].trim();
      }
      // Valid Twitter handle must be 1-15 chars and not a pure numeric ID
      if (raw && /^[A-Za-z0-9_]{1,15}$/.test(raw) && !/^\d+$/.test(raw)) {
        handles.push(raw);
      }
    }
  }

  // Deduplicate
  const unique = [];
  const seen = new Set();
  for (const h of handles) {
    const lower = h.toLowerCase();
    if (!seen.has(lower)) {
      seen.add(lower);
      unique.push(h);
    }
  }
  return unique;
}

async function handleCampaignCSVUpload(input, prefix) {
  const file = input.files && input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const handles = parseCSVHandles(text);
    const statusEl = document.getElementById(`${prefix}-csv-status`) || document.getElementById(`${prefix}-csv-upload-status`);
    if (!handles.length) {
      toast('No valid Twitter handles found in CSV file', 'error');
      if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.innerHTML = `<span style="color:#ef4444;">❌ No valid handles detected in ${esc(file.name)}</span>`;
      }
      campaignCsvHandles[prefix] = [];
      return;
    }
    campaignCsvHandles[prefix] = handles;
    if (statusEl) {
      statusEl.style.display = 'block';
      statusEl.innerHTML = `<span style="color:#10b981;">✅ <strong>${handles.length}</strong> unique handles loaded from <em>${esc(file.name)}</em></span>`;
    }
    toast(`Loaded ${handles.length} handles from ${file.name}`, 'success');
  } catch (err) {
    toast(`Error reading CSV: ${err.message}`, 'error');
  }
}

async function handleCsvUpload(event, prefix) {
  const input = event.target || event;
  await handleCampaignCSVUpload(input, prefix);
}

function dropCampaignCSV(e, prefix) {
  e.preventDefault();
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  const fakeInput = { files: [file] };
  handleCampaignCSVUpload(fakeInput, prefix);
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function toggleScraperTargetType(val) {
  const lbl = document.getElementById('lbl-scraper-target-input');
  const inp = document.getElementById('f-targets');
  const hint = document.getElementById('hint-scraper-target-input');
  if (!lbl || !inp || !hint) return;

  if (val === 'tweet_commenters') {
    lbl.innerHTML = 'Target Tweet URL or ID <span class="badge-required">required</span>';
    inp.placeholder = 'https://x.com/elonmusk/status/1880000000000000000 or 1880000000000000000';
    hint.textContent = 'Direct URL of a tweet or its numeric ID. Scrapes active commenters & repliers on that tweet.';
  } else if (val === 'target_tweets_commenters') {
    lbl.innerHTML = 'Target Profile(s) <span class="badge-required">required</span>';
    inp.placeholder = 'elonmusk, OpenAI, vitalikbuterin (comma-separated)';
    hint.textContent = 'Target profile username(s). Automatically discovers their recent top tweets and scrapes active commenters.';
  } else {
    lbl.innerHTML = 'Target Profile(s) <span class="badge-required">required</span>';
    inp.placeholder = 'elonmusk, OpenAI, nasa (comma-separated)';
    hint.textContent = 'Comma-separated usernames. Streamingly scrapes followers without saving CSV files on disk.';
  }
}

// ══ Scraping ══════════════════════════════════════════════════════════════════
async function startScrape(type) {
  const accountIds = getSelectedAccountIds(type);
  if (!accountIds.length) {
    toast('Please select at least one active Twitter account to scrape with', 'error');
    return;
  }

  let payload = { account_ids: accountIds };

  if (type === 'followers') {
    const targetType = val('f-target-type') || 'followers';
    const rawTargets = (val('f-targets') || '').trim();

    if (!rawTargets) {
      if (targetType === 'tweet_commenters') {
        toast('Please enter a target Tweet URL or numeric Tweet ID', 'error');
      } else {
        toast('Please enter at least one target Twitter profile handle (e.g. elonmusk)', 'error');
      }
      return;
    }

    if (targetType === 'tweet_commenters') {
      const isUrl = rawTargets.includes('/status/') || rawTargets.includes('status/');
      const isNumeric = /^\d{5,}$/.test(rawTargets);
      if (!isUrl && !isNumeric) {
        toast('Invalid Tweet format: Please provide a full Tweet URL (e.g. https://x.com/user/status/123...) or numeric Tweet ID', 'error');
        return;
      }
    } else {
      if (rawTargets.includes('/status/')) {
        toast('You entered a Tweet URL. Please switch Target Type to "Commenters of a Specific Tweet" or enter usernames here.', 'error');
        return;
      }
    }

    const limitVal = parseInt(val('f-limit') || '100');
    if (isNaN(limitVal) || limitVal < 1) {
      toast('Scrape limit must be a positive number of at least 1', 'error');
      return;
    }

    const minF = parseInt(val('f-min-followers') || '0');
    const maxF = parseInt(val('f-max-followers') || '1000');
    if (minF < 0) {
      toast('Min followers cannot be negative', 'error');
      return;
    }
    if (maxF > 0 && minF > maxF) {
      toast(`Min followers (${minF}) cannot be greater than Max followers (${maxF})`, 'error');
      return;
    }

    payload.targets = rawTargets;
    payload.target_type = targetType;
    payload.limit = limitVal;
    payload.min_followers = minF;
    payload.max_followers = maxF;
    payload.country_filter = (typeof campaignSelectedCountries !== 'undefined' && campaignSelectedCountries['f']) ? campaignSelectedCountries['f'].join(',') : '';
  } else if (type === 'search') {
    const q = (val('s-query') || '').trim();
    if (!q) {
      toast('Please enter a search keyword, hashtag, or query', 'error');
      return;
    }
    const limitVal = parseInt(val('s-limit') || '100');
    if (isNaN(limitVal) || limitVal < 1) {
      toast('Search limit must be at least 1', 'error');
      return;
    }
    payload = {
      ...payload,
      query: q,
      since: val('s-since'),
      until: val('s-until'),
      from_users: val('s-from-users'),
      lang: val('s-lang'),
      min_likes: val('s-min-likes'),
      min_retweets: val('s-min-rt'),
      has_images: document.getElementById('s-has-images').value === 'true' ? true : null,
      display_type: val('s-display'),
      limit: limitVal,
    };
  } else if (type === 'profile') {
    const targets = (val('p-targets') || '').trim();
    if (!targets) {
      toast('Please enter at least one Twitter profile username to fetch timeline', 'error');
      return;
    }
    const limitVal = parseInt(val('p-limit') || '100');
    if (isNaN(limitVal) || limitVal < 1) {
      toast('Timeline limit must be at least 1', 'error');
      return;
    }
    payload.targets = targets;
    payload.limit = limitVal;
  }

  const res = await api(`/api/scrape/${type}`, { method: 'POST', body: JSON.stringify(payload) });
  if (res.error) { toast(res.error, 'error'); return; }

  currentJobId = res.job_id;
  toast(`Scrape job started: ${res.job_id.slice(0, 8)} 🚀`, 'success');
  showScraperFeed(type, res.job_id);
  startJobPoll();
  loadScraperJobsTable();
}

function getSelectedAccountIds(prefix) {
  const mapPrefix = { followers: 'f', search: 's', profile: 'p' };
  const p = mapPrefix[prefix] || prefix;
  const el = document.getElementById(`${p}-accounts`);
  if (!el) return accounts.map(a => a.id);
  return Array.from(el.selectedOptions).map(o => parseInt(o.value));
}

function showScraperFeed(type, jobId) {
  const pill = document.getElementById('scraper-status-pill');
  if (pill) { pill.textContent = 'running'; pill.setAttribute('data-s', 'running'); }
  const typeEl = document.getElementById('stat-scrape-type');
  if (typeEl) typeEl.textContent = type ? type.toUpperCase() : '--';
  const cntEl = document.getElementById('stat-scrape-count');
  if (cntEl) cntEl.textContent = '0';
  const matchEl = document.getElementById('stat-scrape-matched');
  if (matchEl) matchEl.textContent = '0';
  const dlBtn = document.getElementById('scraper-download-btn');
  if (dlBtn) dlBtn.style.display = 'none';

  const log = document.getElementById('scraper-live-log');
  if (log) {
    log.innerHTML = `<div class="log-entry log-info"><span class="log-ts">${new Date().toLocaleTimeString()}</span>🚀 Job ${jobId.slice(0, 8)} started (${type}). Initializing scraper pool...</div>`;
  }
}

function startJobPoll() {
  if (jobPollInterval) clearInterval(jobPollInterval);
  jobPollInterval = setInterval(pollJob, 1500);
}

async function pollJob() {
  if (!currentJobId) return;
  const data = await api(`/api/jobs/${currentJobId}`);
  if (!data || data.error) return;
  updateScraperFeedUI(data);

  if (['done', 'error'].includes(data.status)) {
    clearInterval(jobPollInterval);
    loadScraperJobsTable();
    if (data.status === 'done') {
      const dlBtn = document.getElementById('scraper-download-btn');
      if (dlBtn) dlBtn.style.display = 'inline-flex';
      toast('Scrape job completed successfully! 🎉', 'success');
      loadCSVFiles();
      loadCSVSelect();
    }
  }
}

function updateScraperFeedUI(data) {
  const pill = document.getElementById('scraper-status-pill');
  if (pill) {
    pill.textContent = data.status || 'idle';
    pill.setAttribute('data-s', data.status || 'idle');
  }
  const typeEl = document.getElementById('stat-scrape-type');
  if (typeEl && data.type) typeEl.textContent = data.type.toUpperCase();

  const log = document.getElementById('scraper-live-log');
  if (log && data.log && Array.isArray(data.log)) {
    let rawItemsCount = 0;
    let matchedCount = 0;

    const formattedHTML = data.log.map(e => {
      const msg = e.msg || '';
      let cls = 'log-info';
      if (msg.includes('ERROR') || msg.includes('failed') || msg.includes('🚨')) cls = 'log-error';
      else if (msg.includes('WARNING') || msg.includes('RATE LIMIT') || msg.includes('⏳')) cls = 'log-warning';
      else if (msg.includes('MATCH') || msg.includes('✓') || msg.includes('Done') || msg.includes('Saved')) cls = 'log-success';

      // Parse verified matches (monotonic, strictly accurate):
      const mMatch = msg.match(/\[(\d+)\/\d+\]\s+@[\w\d_]+.*✓ MATCH/);
      if (mMatch) {
        matchedCount = Math.max(matchedCount, parseInt(mMatch[1]));
      }
      const mSummary = msg.match(/(\d+)\s+matched\s+criteria/i);
      if (mSummary) {
        matchedCount = parseInt(mSummary[1]);
      }
      const mSaved = msg.match(/(\d+)\s+matched\s+profiles\s+saved/i);
      if (mSaved) {
        matchedCount = parseInt(mSaved[1]);
      }

      // Raw items count:
      const mRaw = msg.match(/Scraped\s+(\d+)\s+total/i);
      if (mRaw) rawItemsCount = Math.max(rawItemsCount, parseInt(mRaw[1]));
      const mFilterRaw = msg.match(/filtering\s+(\d+)\s+raw/i);
      if (mFilterRaw) rawItemsCount = Math.max(rawItemsCount, parseInt(mFilterRaw[1]));

      return `<div class="log-entry ${cls}"><span class="log-ts">${esc(e.ts || '')}</span>${esc(msg)}</div>`;
    }).join('');

    const isAtBottom = (log.scrollHeight - log.scrollTop - log.clientHeight) < 80;
    log.innerHTML = formattedHTML;
    if (isAtBottom) log.scrollTop = log.scrollHeight;

    const cntEl = document.getElementById('stat-scrape-count');
    if (cntEl) cntEl.textContent = rawItemsCount;
    const matchEl = document.getElementById('stat-scrape-matched');
    if (matchEl) matchEl.textContent = matchedCount;
  }

  if (data.result_file) {
    const dlBtn = document.getElementById('scraper-download-btn');
    if (dlBtn) dlBtn.style.display = 'inline-flex';
  }
}

function copyScraperLogs() {
  const log = document.getElementById('scraper-live-log');
  if (!log) return;
  const text = log.innerText || '';
  if (!text.trim()) { toast('No logs to copy', 'info'); return; }
  
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => {
      toast('Scraping logs copied to clipboard! 📋', 'success');
    }).catch(() => fallbackCopyText(text));
  } else {
    fallbackCopyText(text);
  }
}

function fallbackCopyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand('copy');
    toast('Scraping logs copied to clipboard! 📋', 'success');
  } catch (e) {
    toast('Failed to copy logs', 'error');
  }
  document.body.removeChild(ta);
}

function clearScraperFeed() {
  if (jobPollInterval) clearInterval(jobPollInterval);
  currentJobId = null;
  const pill = document.getElementById('scraper-status-pill');
  if (pill) { pill.textContent = 'idle'; pill.setAttribute('data-s', 'idle'); }
  const cntEl = document.getElementById('stat-scrape-count');
  if (cntEl) cntEl.textContent = '--';
  const matchEl = document.getElementById('stat-scrape-matched');
  if (matchEl) matchEl.textContent = '--';
  const typeEl = document.getElementById('stat-scrape-type');
  if (typeEl) typeEl.textContent = '--';
  const dlBtn = document.getElementById('scraper-download-btn');
  if (dlBtn) dlBtn.style.display = 'none';

  const log = document.getElementById('scraper-live-log');
  if (log) {
    log.innerHTML = '<div class="log-entry log-info">No active scraping job running. Configure parameters on the left and click "Start Scraping" to monitor live scraping logs.</div>';
  }
  toast('Scraper live feed cleared', 'info');
}

async function loadScraperJobsTable() {
  const tbody = document.getElementById('scraper-jobs-tbody');
  if (!tbody) return;
  try {
    const jobs = await api('/api/jobs');
    if (!jobs || !Array.isArray(jobs) || !jobs.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty-row">No scraping jobs recorded yet</td></tr>';
      return;
    }

    tbody.innerHTML = jobs.map(j => {
      let paramsSummary = '--';
      try {
        const p = typeof j.params === 'string' ? JSON.parse(j.params || '{}') : (j.params || {});
        if (p.targets) paramsSummary = `@${p.targets}`;
        else if (p.query) paramsSummary = `Query: "${p.query}"`;
        else if (p.target) paramsSummary = `@${p.target}`;
      } catch (e) {
        paramsSummary = String(j.params || '--');
      }

      const resFile = j.result_file ? j.result_file.split(/[\/\\]/).pop() : '--';
      const createdStr = j.created_at ? new Date(j.created_at).toLocaleString() : '--';
      const isDone = j.status === 'done';

      return `
        <tr>
          <td><strong>#${j.id.slice(0, 8)}</strong></td>
          <td><span class="badge badge-secondary" style="text-transform:uppercase;font-size:11px;">${esc(j.type || 'SCRAPE')}</span></td>
          <td><span class="campaign-status-pill" data-s="${esc(j.status)}">${esc(j.status)}</span></td>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(paramsSummary)}">${esc(paramsSummary)}</td>
          <td style="font-family:monospace;font-size:12px;color:var(--blue);">${esc(resFile)}</td>
          <td style="font-size:12px;color:var(--text-3);">${esc(createdStr)}</td>
          <td>
            <div style="display:flex;gap:6px;">
              ${isDone ? `<button class="btn btn-sm btn-ghost" onclick="downloadJobById('${j.id}')">⬇ CSV</button>` : ''}
              <button class="btn btn-sm btn-ghost" onclick="viewJobInFeed('${j.id}')">👁 View Log</button>
              <button class="btn btn-sm btn-danger" style="padding:2px 6px;font-size:11px;" onclick="deleteJobById('${j.id}')">✕</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    console.warn("Failed to load scraper jobs table:", err);
  }
}

async function downloadJobById(jobId) {
  window.location.href = `/api/jobs/${jobId}/download`;
}

async function viewJobInFeed(jobId) {
  currentJobId = jobId;
  const data = await api(`/api/jobs/${jobId}`);
  if (data && !data.error) {
    updateScraperFeedUI(data);
    const log = document.getElementById('scraper-live-log');
    if (log) log.scrollIntoView({ behavior: 'smooth' });
    toast(`Loaded log for Job #${jobId.slice(0, 8)}`, 'info');
  }
}

async function deleteJobById(jobId) {
  if (!confirm(`Delete scraping job #${jobId.slice(0, 8)}?`)) return;
  const res = await api(`/api/jobs/${jobId}`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('Job deleted', 'info');
  if (currentJobId === jobId) clearScraperFeed();
  loadScraperJobsTable();
  loadCSVFiles();
}

async function downloadJob() {
  if (!currentJobId) return;
  window.location.href = `/api/jobs/${currentJobId}/download`;
}

// ══ CSV upload ════════════════════════════════════════════════════════════════
async function uploadCSV(input) {
  const file = input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/api/upload-csv', { method: 'POST', body: form }).then(r => r.json());
  if (res.error) { toast(res.error, 'error'); return; }
  document.getElementById('csv-upload-status').textContent = `Uploaded: ${res.name}`;
  toast(`CSV uploaded: ${res.name}`, 'success');
  loadCSVFiles();
  loadCSVSelect();
}

function dropCSV(e) {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (!file) return;
  const fakeInput = { files: [file] };
  uploadCSV(fakeInput);
}

async function loadCSVFiles() {
  const data = await api('/api/csv-files');
  const container = document.getElementById('csv-files-list');
  if (!container) return;
  if (!data.length) {
    container.innerHTML = '<p style="color:var(--text-3);font-size:13px">No CSV files yet. Run a scrape first.</p>';
    return;
  }
  container.innerHTML = data.map(f => `
    <div class="file-chip">
      <span>📄</span>
      <div>
        <div class="fc-name">${esc(f.name)}</div>
        <div class="fc-size">${f.size_kb} KB · ${esc(f.folder)}</div>
      </div>
    </div>
  `).join('');
}

async function loadCSVSelect() {
  const data = await api('/api/csv-files');
  const sel = document.getElementById('c-csv-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">— Select a CSV file —</option>' +
    data.map(f => `<option value="${esc(f.path)}">${esc(f.name)} (${f.size_kb} KB)</option>`).join('');
}

// ══ Jobs (Results tab) ═══════════════════════════════════════════════════════
async function loadJobs() {
  const data = await api('/api/jobs');
  const tbody = document.getElementById('jobs-tbody');
  if (!tbody) return;
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-row">No jobs yet</td></tr>';
    return;
  }
  tbody.innerHTML = data.map(j => `
    <tr>
      <td><strong>${esc(j.label || j.type)}</strong></td>
      <td>${esc(j.type)}</td>
      <td><span class="status-pill" data-status="${j.status}">${j.status}</span></td>
      <td>${fmtDate(j.created_at)}</td>
      <td>
        ${j.result_file
      ? `<button class="btn btn-sm btn-ghost" onclick="window.location.href='/api/jobs/${j.id}/download'">⬇ CSV</button>`
      : '—'}
        <button class="btn btn-sm btn-ghost" onclick="viewJob('${j.id}')">👁 View</button>
        <button class="btn btn-sm btn-danger" onclick="deleteJob('${j.id}')">✕</button>
      </td>
    </tr>
  `).join('');
}

async function viewJob(id) {
  const data = await api(`/api/jobs/${id}`);
  currentJobId = id;
  switchTab('scraper', document.querySelector('[data-tab="scraper"]'));
  showJobMonitor();
  updateJobUI(data);
}

async function deleteJob(id) {
  if (!confirm('Delete this scrape job and its CSV file?')) return;
  const res = await api(`/api/jobs/${id}`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('Job deleted', 'info');
  loadJobs();
  loadCSVFiles();
}

async function deleteAllJobs() {
  if (!confirm('Permanently delete ALL scraping jobs and their generated CSV files?')) return;
  const res = await api('/api/jobs/all', { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('All scraping jobs deleted', 'info');
  clearScraperFeed();
  loadScraperJobsTable();
  loadCSVFiles();
}

// ══ Image Generation (local tweet card template) ════════════════════════════
async function generateCardImage() {
  const display_name = val('c-display-name');
  const username     = val('c-username');
  const body_text    = val('c-body-text');

  if (!display_name) { toast('Name is required', 'error'); return; }
  if (!body_text)    { toast('Body text is required', 'error'); return; }

  toast('Generating card...', 'info');

  const formData = new FormData();
  formData.append('display_name', display_name);
  formData.append('username', username || display_name.toLowerCase().replace(/\s+/g, ''));
  formData.append('body_text', body_text);
  formData.append('timestamp', val('c-timestamp'));
  formData.append('likes', val('c-likes'));
  formData.append('retweets', val('c-retweets'));
  formData.append('replies', val('c-replies'));
  formData.append('views', val('c-views'));

  const avatarInput = document.getElementById('c-avatar-file');
  if (avatarInput && avatarInput.files[0]) {
    formData.append('avatar', avatarInput.files[0]);
  }

  try {
    const res = await fetch('/api/image/generate-card', {
      method: 'POST',
      body: formData,
    }).then(r => r.json());

    if (res.error) { toast('Card generation failed: ' + res.error, 'error'); return; }

    const wrap = document.getElementById('image-preview-wrap');
    const img  = document.getElementById('image-preview');
    img.src    = res.preview_url + '?t=' + Date.now();
    wrap.style.display = 'block';
    toast('Card generated successfully!', 'success');
  } catch (e) {
    toast('Card error: ' + e.message, 'error');
  }
}

async function generateImage() { return generateCardImage(); }

// ══ Campaign ══════════════════════════════════════════════════════════════════
async function startCampaign() {
  const checkedBoxes = Array.from(document.querySelectorAll('.chk-account:checked')).map(cb => parseInt(cb.value));
  const selectOpts = document.getElementById('c-accounts') ? Array.from(document.getElementById('c-accounts').selectedOptions).map(o => parseInt(o.value)) : [];
  const accountIds = Array.from(new Set([...checkedBoxes, ...selectOpts])).filter(id => !isNaN(id));
  if (!accountIds.length) { toast('Select at least one posting account in Step 5', 'error'); return; }

  const target_type = val('c-target-type') || 'followers';
  let source_profiles = val('c-source-profiles');
  let csv_handles = [];

  if (target_type === 'csv_list') {
    csv_handles = campaignCsvHandles['c'] || [];
    if (!csv_handles.length) {
      toast('Please upload a CSV file containing Twitter handles in Step 5', 'error');
      return;
    }
    source_profiles = `CSV (${csv_handles.length} handles)`;
  } else if (target_type === 'tweet_commenters') {
    if (!source_profiles || !source_profiles.trim()) {
      toast('Please enter a Tweet URL or numeric Tweet ID in Step 5', 'error');
      return;
    }
    const trimmed = source_profiles.trim();
    const isUrl = /(?:twitter\.com|x\.com)\/[^/]+\/status\/(\d+)/i.test(trimmed);
    const isNumericId = /^\d{5,25}$/.test(trimmed);
    if (!isUrl && !isNumericId) {
      toast('Invalid Tweet link or ID format. Enter a valid post URL (e.g. https://x.com/user/status/123456789) or numeric Tweet ID.', 'error');
      return;
    }
  } else {
    if (!source_profiles || !source_profiles.trim()) {
      toast('Enter at least one source profile to scrape from', 'error');
      return;
    }
    const handles = source_profiles.split(/[\s,]+/).map(h => h.trim().replace(/^@+/, '')).filter(Boolean);
    for (const h of handles) {
      if (!/^[A-Za-z0-9_]{1,15}$/.test(h)) {
        toast(`Invalid Twitter handle format: '@${h}'. Usernames can only contain letters, numbers, and underscores (max 15 chars).`, 'error');
        return;
      }
    }
  }

  const display_name = val('c-display-name') || '';
  const body_text = val('c-body-text') || '';
  const posting_mode = val('c-posting-mode') || 'list_card';
  const post_template = val('c-post-template') || '';
  const normal_media_data = (typeof campaignCustomImageData !== 'undefined' && campaignCustomImageData['c']) ? campaignCustomImageData['c'] : null;
  const country_filter = (typeof campaignSelectedCountries !== 'undefined' && campaignSelectedCountries['c']) ? campaignSelectedCountries['c'].join(',') : '';
  const username = val('c-username') || (display_name ? display_name.toLowerCase().replace(/\s+/g, '') : '');

  // Validate Tweet Template (Step 3) for all posting modes
  if (!post_template || !post_template.trim()) {
    toast('Please enter a Tweet Template in Step 3', 'error');
    return;
  }
  if (!post_template.includes('{taggings}')) {
    toast('Tweet Template in Step 3 must include the {taggings} placeholder where target usernames will be inserted.', 'error');
    return;
  }

  // Validate Posting Mode Specific Required Parameters
  if (posting_mode === 'list_card' || posting_mode === 'normal_card') {
    if (!display_name || !display_name.trim()) {
      toast('Please enter a Name / Display Name in Step 1 for the Generated Card Image', 'error');
      return;
    }
    if (!body_text || !body_text.trim()) {
      toast('Please enter the Tweet Body text in Step 1 for the Generated Card Image', 'error');
      return;
    }
  } else if (posting_mode === 'normal_custom') {
    if (!normal_media_data) {
      toast('Please upload a Custom Media Image in Step 2 for Normal Custom Image mode', 'error');
      return;
    }
  }

  const config = {
    account_ids: accountIds,
    target_type,
    source_profiles,
    csv_handles,
    display_name,
    username,
    body_text,
    timestamp:         val('c-timestamp'),
    verified:          document.getElementById('c-verified')?.value === 'true',
    likes:             val('c-likes'),
    retweets:          val('c-retweets'),
    replies:           val('c-replies'),
    views:             val('c-views'),
    posting_mode,
    normal_media_data,
    update_list_banner: (posting_mode === 'list_card'),
    list_name:         val('c-list-name') || 'Official Notice',
    list_description:  val('c-list-desc'),
    post_template,
    tags_per_post:     parseInt(document.getElementById('c-tags-per-post')?.value || '3'),
    min_delay_minutes: parseInt(val('c-min-delay') || '8'),
    max_delay_minutes: parseInt(val('c-max-delay') || '20'),
    max_posts_per_account: parseInt(val('c-max-posts') || '30'),
    min_followers:     parseInt(val('c-min-followers') || '0'),
    max_followers:     parseInt(val('c-max-followers') || '1000'),
    country_filter,
    execution_mode:    val('c-execution-mode') || 'vps',
  };

  const name = val('c-name') || ('Campaign ' + new Date().toLocaleString());

  // Create campaign
  const created = await api('/api/campaigns', {
    method: 'POST',
    body: JSON.stringify({ name, config }),
  });
  if (created.error) { toast(created.error, 'error'); return; }

  currentCampaignId = created.id;

  // Start campaign
  const started = await api(`/api/campaigns/${created.id}/start`, { method: 'POST' });
  if (started.error) { toast(started.error, 'error'); return; }

  toast('Campaign launched! 🚀', 'success');
  currentCampaignId = created.id;
  startCampaignPoll();
  loadCampaignDbPanel();
}

function updateCampaignStats(config) {
  // Stats now live on per-campaign cards rendered by renderActiveCampaignFeeds()
}

async function stopCampaign() {
  if (!currentCampaignId) { toast('No active campaign selected', 'error'); return; }
  await stopCampaignById(currentCampaignId);
}

async function stopCampaignId(cid) {
  await stopCampaignById(cid);
}

async function stopCampaignById(cid) {
  await api(`/api/campaigns/${cid}/stop`, { method: 'POST' });
  toast(`Campaign #${cid} stopped`, 'info');
  const cardEl = document.getElementById(`card-live-${cid}`);
  if (cardEl) {
    cardEl.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
    cardEl.style.opacity = '0';
    cardEl.style.transform = 'translateY(-10px)';
    setTimeout(() => cardEl.remove(), 300);
  }
  setTimeout(pollCampaign, 400);
  loadCampaignDbPanel();
}

async function resumeCampaign(cid) {
  await resumeCampaignById(cid);
}

async function resumeCampaignById(cid) {
  currentCampaignId = cid;
  const res = await api(`/api/campaigns/${cid}/resume`, { method: 'POST' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`Campaign #${cid} resumed!`, 'success');
  startCampaignPoll();
  loadCampaignDbPanel();
}

async function selectCampaignForLogs(cid) {
  currentCampaignId = cid;
  startCampaignPoll();
  toast(`Viewing logs for Campaign #${cid}`, 'info');
}

async function resumeActiveCampaign() {
  if (currentCampaignId) resumeCampaignById(currentCampaignId);
}

function openEditCampaignModalById(cid) {
  currentCampaignId = cid;
  openEditCampaignModal();
}

async function clearCampaignTaggedById(cid) {
  if (!confirm(`Clear tagged history for campaign #${cid}?`)) return;
  const res = await api(`/api/campaigns/${cid}/tagged`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`Cleared tagged users for campaign #${cid}`, 'success');
  const el = document.getElementById(`stat-tagged-${cid}`);
  if (el) el.textContent = '0';
  loadCampaignDbPanel();
}



function startCampaignPoll() {
  if (campaignPollInterval) clearInterval(campaignPollInterval);
  pollCampaign();
  campaignPollInterval = setInterval(pollCampaign, 3000);
}

async function pollCampaign() {
  try {
    const listRes = await api('/api/campaigns');
    if (!listRes || listRes.error || !Array.isArray(listRes)) return;

    // Active campaigns: only actively running campaigns appear in the live feed column (max 3)
    const active = listRes.filter(c => c.status === 'running').slice(0, 3);

    renderActiveCampaignFeeds(active);

    for (const c of active) {
      await pollSingleCampaignData(c.id, c.account_ids_count || (c.config ? (c.config.account_ids || []).length : 0));
    }
  } catch (err) {
    console.warn("Campaigns poll failed:", err);
  }
}

function renderActiveCampaignFeeds(activeCampaigns) {
  const col = document.getElementById('live-campaign-feeds-col');
  if (!col) return;

  if (activeCampaigns.length === 0) {
    col.innerHTML = `
      <div class="card card-live" id="card-live-idle">
        <div class="card-title">Live Campaign Feed
          <span class="campaign-status-pill" data-s="idle">idle</span>
        </div>
        <div class="log-panel" style="min-height:140px;">
          <div class="log-entry log-info">No active campaigns running. Configure details on the left and click "Launch Campaign" to start.</div>
        </div>
      </div>
    `;
    return;
  }

  const activeIds = activeCampaigns.map(c => c.id);
  const existingCards = Array.from(col.querySelectorAll('.card-live'));

  // Remove stopped or deleted campaign cards
  existingCards.forEach(cardEl => {
    const id = parseInt(cardEl.id.replace('card-live-', ''));
    if (!isNaN(id) && !activeIds.includes(id)) {
      cardEl.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
      cardEl.style.opacity = '0';
      cardEl.style.transform = 'translateY(-10px)';
      setTimeout(() => cardEl.remove(), 300);
    }
  });

  // Remove idle placeholder if present
  const idleCard = document.getElementById('card-live-idle');
  if (idleCard && activeCampaigns.length > 0) idleCard.remove();

  // Render or update active campaign cards
  activeCampaigns.forEach((c) => {
    let cardEl = document.getElementById(`card-live-${c.id}`);
    if (!cardEl) {
      cardEl = document.createElement('div');
      cardEl.className = 'card card-live';
      cardEl.id = `card-live-${c.id}`;
      cardEl.innerHTML = buildCampaignFeedCardHTML(c);
      col.appendChild(cardEl);
    } else {
      const pill = document.getElementById(`camp-status-pill-${c.id}`);
      if (pill) {
        pill.textContent = c.status;
        pill.setAttribute('data-s', c.status);
      }
      const resumeBtn = document.getElementById(`resume-btn-${c.id}`);
      const stopBtn = document.getElementById(`stop-btn-${c.id}`);
      if (c.status === 'running') {
        if (stopBtn) stopBtn.style.display = 'inline-flex';
        if (resumeBtn) resumeBtn.style.display = 'none';
      } else {
        if (stopBtn) stopBtn.style.display = 'none';
        if (resumeBtn) resumeBtn.style.display = 'inline-flex';
      }
    }
  });
}

function buildCampaignFeedCardHTML(c) {
  const cid = c.id;
  const isRunning = c.status === 'running';
  const name = c.name || `Campaign #${cid}`;
  const accCount = (c.config && c.config.account_ids) ? c.config.account_ids.length : '--';
  return `
    <div class="card-title">Live Campaign Feed — ${esc(name)}
      <span class="campaign-status-pill" id="camp-status-pill-${cid}" data-s="${c.status}">${c.status}</span>
    </div>
    <div class="campaign-stats">
      <div class="stat-box"><div class="stat-num" id="stat-posts-${cid}">--</div><div class="stat-label">Posts sent</div></div>
      <div class="stat-box"><div class="stat-num" id="stat-tagged-${cid}">--</div><div class="stat-label">Users tagged</div></div>
      <div class="stat-box"><div class="stat-num" id="stat-accounts-${cid}">${accCount}</div><div class="stat-label">Active accounts</div></div>
    </div>
    <div class="log-panel" id="campaign-log-${cid}">
      <div class="log-entry log-info">Loading campaign logs...</div>
    </div>
    <div class="form-actions" style="margin-top:12px;gap:8px;display:flex;flex-wrap:wrap;">
      <button class="btn btn-ghost" onclick="copyCampaignLogsById(${cid})">📋 Copy Logs</button>
      <button class="btn btn-secondary" onclick="openEditCampaignModalById(${cid})">✏ Edit Campaign</button>
      <button class="btn btn-success" id="resume-btn-${cid}" style="display:${isRunning ? 'none' : 'inline-flex'}" onclick="resumeCampaignById(${cid})">▶ Resume Campaign</button>
      <button class="btn btn-danger" id="stop-btn-${cid}" style="display:${isRunning ? 'inline-flex' : 'none'}" onclick="stopCampaignById(${cid})">Stop Campaign</button>
      <button class="btn btn-ghost" onclick="clearCampaignTaggedById(${cid})">🗑 Clear Tagged Users</button>
    </div>
  `;
}

async function pollSingleCampaignData(cid) {
  try {
    const data = await api(`/api/campaigns/${cid}/log`);
    if (data && !data.error) {
      updateCampaignLogById(cid, data);
    }
    const tc = await api(`/api/campaigns/${cid}/tagged-count`);
    if (tc && !tc.error) {
      const el = document.getElementById(`stat-tagged-${cid}`);
      if (el) el.textContent = tc.tagged_count;
    }
  } catch (err) {
    console.warn(`Campaign #${cid} poll error:`, err);
  }
}

function updateCampaignLogById(cid, data) {
  const pill = document.getElementById(`camp-status-pill-${cid}`);
  if (pill) { pill.textContent = data.status; pill.setAttribute('data-s', data.status); }

  const log = document.getElementById(`campaign-log-${cid}`);
  if (!log || !data.log) return;

  const postCount = data.log.filter(e => e.level === 'SUCCESS' && (e.msg.includes('Tweeted:') || e.msg.includes('Posted:'))).length;
  const postEl = document.getElementById(`stat-posts-${cid}`);
  if (postEl) postEl.textContent = postCount || '0';

  const lastEntry = data.log.length ? JSON.stringify(data.log[data.log.length - 1]) : '';
  if (log.dataset.lastEntry === lastEntry) return;
  log.dataset.lastEntry = lastEntry;

  const isAtBottom = (log.scrollHeight - log.scrollTop - log.clientHeight) < 80;

  log.innerHTML = data.log.map(e => {
    const cls = {
      INFO: 'log-info', SUCCESS: 'log-success', ERROR: 'log-error',
      WARNING: 'log-warning', POST: 'log-post',
    }[e.level] || 'log-info';
    return `<div class="log-entry ${cls}"><span class="log-ts">${e.ts}</span>${esc(e.msg)}</div>`;
  }).join('');

  if (isAtBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

function copyCampaignLogsById(cid) {
  const log = document.getElementById(`campaign-log-${cid}`);
  if (!log) return;
  const entries = Array.from(log.querySelectorAll('.log-entry'));
  if (!entries.length) { toast('No logs to copy', 'info'); return; }
  const text = entries.map(el => el.textContent).join('\n');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast('Logs copied! 📋', 'success')).catch(() => fallbackCopyText(text));
  } else {
    fallbackCopyText(text);
  }
}

function copyCampaignLogs() {
  if (currentCampaignId) copyCampaignLogsById(currentCampaignId);
}

function fallbackCopyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
    toast('Logs copied to clipboard! 📋', 'success');
  } catch (e) {
    toast('Could not copy logs: ' + e.message, 'error');
  }
  document.body.removeChild(ta);
}

async function loadLists() {
  const data = await api('/api/lists');
  const section = document.getElementById('lists-section');
  const feed = document.getElementById('lists-feed');
  if (!data.length) return;
  section.style.display = 'block';
  feed.innerHTML = data.map(l => `
    <div class="list-item">
      <span>📋</span>
      <a href="${esc(l.list_url)}" target="_blank">${esc(l.list_name)}</a>
      <span style="color:var(--text-3);font-size:11px">Account #${l.account_id}</span>
    </div>
  `).join('');
}

// ══ Tagged user management ══════════════════════════════════════════════════
async function clearTaggedUsers() {
  if (!currentCampaignId) { toast('No active campaign selected', 'error'); return; }
  if (!confirm(`Clear all tagged usernames for campaign #${currentCampaignId}? The campaign will start re-tagging from scratch on next launch.`)) return;
  const res = await api(`/api/campaigns/${currentCampaignId}/tagged`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('Tagged users cleared for this campaign', 'success');
  document.getElementById('stat-tagged').textContent = '0';
  loadCampaignDbPanel();
}

async function clearAllTagged() {
  if (!confirm('Clear ALL tagged usernames across ALL campaigns? This cannot be undone.')) return;
  const res = await api('/api/campaigns/tagged/all', { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('All tagged user history cleared', 'success');
  document.getElementById('stat-tagged').textContent = '0';
  loadCampaignDbPanel();
}

async function autoConnectActiveCampaign() {
  try {
    const campaigns = await api('/api/campaigns');
    if (!campaigns || !Array.isArray(campaigns)) return;
    const running = campaigns.find(c => c.status === 'running');
    if (running) currentCampaignId = running.id;
    else if (!currentCampaignId && campaigns.length > 0) currentCampaignId = campaigns[0].id;
    // Always start the poll — renderActiveCampaignFeeds() handles all card rendering
    if (!campaignPollInterval) startCampaignPoll();
  } catch (e) {
    console.warn("autoConnectActiveCampaign error:", e);
  }
}

async function loadCampaignDbPanel() {
  await autoConnectActiveCampaign();
  const campaigns = await api('/api/campaigns');
  const container = document.getElementById('campaign-db-list');
  if (!container) return;

  if (!campaigns.length) {
    container.innerHTML = `
      <p style="color:var(--text-3);font-size:13px;margin:0 0 8px;">No campaigns in database.</p>
      <div class="form-actions" style="margin-top:12px;">
        <button class="btn btn-ghost" onclick="loadCampaignDbPanel()">↺ Refresh</button>
      </div>
    `;
    return;
  }
  // Fetch tagged counts for each
  const rows = await Promise.all(
    campaigns.map(async c => {
      const tc = await api(`/api/campaigns/${c.id}/tagged-count`);
      return { ...c, tagged_count: tc.tagged_count ?? 0 };
    })
  );
  container.innerHTML = `
    <table class="data-table" style="width:100%">
      <thead><tr>
        <th>#</th><th>Name</th><th>Engine Mode</th><th>Status</th><th>Tagged</th><th>Actions</th>
      </tr></thead>
      <tbody>
        ${rows.map(c => {
          let mode = 'vps';
          try {
            const cfg = typeof c.config === 'string' ? JSON.parse(c.config) : (c.config || {});
            mode = cfg.execution_mode || 'vps';
          } catch(e){}
          const modeLabel = mode === 'local' ? '💻 Local PC' : '🌐 24/7 VPS Cloud';
          const isRunning = c.status === 'running';
          return `
            <tr>
              <td>${c.id}</td>
              <td>${esc(c.name)}</td>
              <td><span style="font-size:11px;padding:2px 8px;border-radius:4px;background:rgba(29,155,240,0.15);color:#1d9bf0;font-weight:600;">${modeLabel}</span></td>
              <td><span class="status-pill" data-status="${c.status}">${c.status}</span></td>
              <td><strong>${c.tagged_count.toLocaleString()}</strong></td>
              <td style="display:flex;gap:4px;flex-wrap:wrap;">
                <button class="btn btn-sm btn-ghost" onclick="openEditCampaignModal(${c.id})">✏ Edit</button>
                ${isRunning 
                  ? `<button class="btn btn-sm btn-danger" onclick="stopCampaignId(${c.id})">⏹ Stop</button>`
                  : `<button class="btn btn-sm btn-success" onclick="resumeCampaign(${c.id})">▶ Resume</button>`
                }
                <button class="btn btn-sm btn-ghost" onclick="clearCampaignTagged(${c.id})">🗑 Clear</button>
                <button class="btn btn-sm btn-danger" onclick="deleteCampaign(${c.id})">✕ Delete</button>
              </td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
    <div class="form-actions" style="margin-top:12px;gap:8px;display:flex;flex-wrap:wrap;">
      <button class="btn btn-ghost" onclick="loadCampaignDbPanel()">↺ Refresh</button>
      <button class="btn btn-ghost" onclick="clearAllTagged()">🗑 Clear All Tagged Users</button>
      <button class="btn btn-danger" onclick="deleteAllCampaigns()">🗑 Delete All Campaigns</button>
    </div>
  `;
}

// ══ Edit Campaign Modal Handlers ═════════════════════════════════════════════
let editingCampaignId = null;

async function openEditCampaignModal(cid) {
  const targetId = cid || currentCampaignId;
  if (!targetId) { toast('No campaign selected to edit', 'error'); return; }
  editingCampaignId = targetId;

  const data = await api(`/api/campaigns/${targetId}`);
  if (data.error) { toast(data.error, 'error'); return; }

  const cfg = data.config || {};
  set('edit-c-name', data.name || '');
  const modeVal = cfg.posting_mode || (cfg.update_list_banner !== false ? 'list_card' : 'list_static');
  set('edit-c-posting-mode', modeVal);
  toggleEditPostingMode(modeVal);

  const targetTypeVal = cfg.target_type || 'followers';
  set('edit-c-target-type', targetTypeVal);
  toggleEditTargetType(targetTypeVal);
  if (cfg.csv_handles && cfg.csv_handles.length) {
    campaignCsvHandles['edit-c'] = cfg.csv_handles;
    const statusEl = document.getElementById('edit-c-csv-upload-status');
    if (statusEl) {
      statusEl.innerHTML = `<span style="color:#10b981;">✅ <strong>${cfg.csv_handles.length}</strong> handles configured in this campaign</span>`;
    }
  } else {
    campaignCsvHandles['edit-c'] = [];
    const statusEl = document.getElementById('edit-c-csv-upload-status');
    if (statusEl) statusEl.innerHTML = '';
  }

  set('edit-c-source-profiles', cfg.source_profiles || '');
  set('edit-c-min-followers', cfg.min_followers ?? 0);
  set('edit-c-max-followers', cfg.max_followers ?? 1000);

  const cVal = cfg.country_filter || '';
  campaignSelectedCountries['edit-c'] = cVal ? cVal.split(',').map(s => s.trim()).filter(s => s) : [];
  renderCountryChips('edit-c');

  set('edit-c-post-template', cfg.post_template || '');
  set('edit-c-display-name', cfg.display_name || '');
  set('edit-c-username', cfg.username || '');
  set('edit-c-body-text', cfg.body_text || '');
  set('edit-c-update-list-banner', cfg.update_list_banner !== false ? 'true' : 'false');
  set('edit-c-list-name', cfg.list_name || 'Official Notice');
  set('edit-c-tags-per-post', cfg.tags_per_post || 3);
  set('edit-c-min-delay', cfg.min_delay_minutes || 8);
  set('edit-c-max-delay', cfg.max_delay_minutes || 20);
  set('edit-c-max-posts', cfg.max_posts_per_account || 30);
  set('edit-c-cooldown-mins', cfg.cooldown_minutes || 30);

  // Populate accounts multi-select
  const accSel = document.getElementById('edit-c-accounts');
  if (accSel) {
    const selectedIds = (cfg.account_ids && cfg.account_ids.length) ? cfg.account_ids : accounts.map(a => a.id);
    accSel.innerHTML = accounts.map(a =>
      `<option value="${a.id}" ${selectedIds.includes(a.id) ? 'selected' : ''}>
        ${esc(a.label || `Account #${a.id}`)} (${esc(a.token_preview)})
      </option>`
    ).join('');
  }

  document.getElementById('editCampaignModal').style.display = 'block';
}

function closeEditCampaignModal() {
  document.getElementById('editCampaignModal').style.display = 'none';
  editingCampaignId = null;
}

async function saveCampaignEdit() {
  if (!editingCampaignId) return;

  const name = val('edit-c-name');
  if (!name) { toast('Campaign name is required', 'error'); return; }

  const accSel = document.getElementById('edit-c-accounts');
  let account_ids = accSel ? Array.from(accSel.selectedOptions).map(o => parseInt(o.value)) : [];
  if (!account_ids.length && accounts.length) {
    account_ids = accounts.map(a => a.id);
  }
  if (!account_ids.length) { toast('Select at least one posting account in Step 5', 'error'); return; }

  // Fetch current config to merge
  const current = await api(`/api/campaigns/${editingCampaignId}`);
  const prevConfig = current.config || {};

  const editCountryVal = (campaignSelectedCountries['edit-c'] || []).join(', ');

  const editTargetType = val('edit-c-target-type') || 'followers';
  let editSourceProfiles = val('edit-c-source-profiles');
  let editCsvHandles = prevConfig.csv_handles || [];

  if (editTargetType === 'csv_list') {
    editCsvHandles = campaignCsvHandles['edit-c'] && campaignCsvHandles['edit-c'].length ? campaignCsvHandles['edit-c'] : (prevConfig.csv_handles || []);
    if (!editCsvHandles.length) {
      toast('Please upload a CSV file with Twitter handles for CSV campaign', 'error');
      return;
    }
    editSourceProfiles = `CSV (${editCsvHandles.length} handles)`;
  }

  const editPostingMode = val('edit-c-posting-mode') || 'list_card';
  const editDisplayName = val('edit-c-display-name') || '';
  const editBodyText = val('edit-c-body-text') || '';
  const rawPostTemplate = val('edit-c-post-template') || '';

  if (!rawPostTemplate || !rawPostTemplate.trim()) {
    toast('Tweet Template is required in Edit Campaign', 'error');
    return;
  }

  if (editPostingMode === 'list_card' || editPostingMode === 'normal_card') {
    if (!editDisplayName || !editDisplayName.trim()) {
      toast('Display Name in Step 1 is required for Generated Card Image mode', 'error');
      return;
    }
    if (!editBodyText || !editBodyText.trim()) {
      toast('Tweet Body text in Step 1 is required for Generated Card Image mode', 'error');
      return;
    }
  }

  const updatedConfig = {
    ...prevConfig,
    account_ids,
    posting_mode: editPostingMode,
    target_type: editTargetType,
    source_profiles: editSourceProfiles,
    csv_handles: editCsvHandles,
    min_followers: parseInt(val('edit-c-min-followers') || '0'),
    max_followers: parseInt(val('edit-c-max-followers') || '1000'),
    country_filter: editCountryVal,
    post_template: (val('edit-c-post-template') || '').includes('{taggings}') ? val('edit-c-post-template') : (val('edit-c-post-template') + ' {taggings}'),
    display_name: val('edit-c-display-name'),
    username: val('edit-c-username'),
    body_text: val('edit-c-body-text'),
    update_list_banner: (val('edit-c-posting-mode') === 'list_card'),
    list_name: val('edit-c-list-name') || 'Official Notice',
    tags_per_post: parseInt(val('edit-c-tags-per-post') || '3'),
    min_delay_minutes: parseInt(val('edit-c-min-delay') || '8'),
    max_delay_minutes: parseInt(val('edit-c-max-delay') || '20'),
    max_posts_per_account: parseInt(val('edit-c-max-posts') || '30'),
    cooldown_minutes: parseInt(val('edit-c-cooldown-mins') || '30'),
  };

  const res = await api(`/api/campaigns/${editingCampaignId}`, {
    method: 'PUT',
    body: JSON.stringify({ name, config: updatedConfig }),
  });

  if (res.error) { toast(res.error, 'error'); return; }
  toast('Campaign updated successfully!', 'success');
  closeEditCampaignModal();
  loadCampaignDbPanel();
}

async function deleteAllCampaigns() {
  if (!confirm('Permanently delete ALL campaigns and all tagged user history across ALL campaigns?')) return;
  const res = await api('/api/campaigns/all', { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('All campaigns deleted', 'info');
  currentCampaignId = null;
  pollCampaign();
  loadCampaignDbPanel();
}

async function deleteCampaign(cid) {
  if (!confirm(`Permanently delete campaign #${cid} and all its tagged history?`)) return;
  const res = await api(`/api/campaigns/${cid}`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`Campaign #${cid} deleted`, 'info');
  if (currentCampaignId === cid) currentCampaignId = null;
  const cardEl = document.getElementById(`card-live-${cid}`);
  if (cardEl) cardEl.remove();
  pollCampaign();
  loadCampaignDbPanel();
}

async function clearCampaignTagged(cid) {
  await clearCampaignTaggedById(cid);
}

// ══ Utilities ═════════════════════════════════════════════════════════════════
function updateAccountSelectBadge(id) {
  const el = document.getElementById(id);
  const badge = document.getElementById(`${id}-badge`);
  if (!el || !badge) return;
  const total = el.options.length;
  const selected = Array.from(el.selectedOptions).length;
  if (total === 0) {
    badge.textContent = '(No accounts)';
    badge.style.color = 'var(--text-3)';
  } else if (selected === total) {
    badge.textContent = `(${selected} of ${total} selected ✓)`;
    badge.style.color = '#38ef7d';
  } else if (selected > 0) {
    badge.textContent = `(${selected} of ${total} selected)`;
    badge.style.color = 'var(--accent)';
  } else {
    badge.textContent = `(0 of ${total} selected)`;
    badge.style.color = 'var(--text-3)';
  }
}

function selectAllAccounts(id, selectAll = true) {
  const el = document.getElementById(id);
  if (!el) return;
  for (let i = 0; i < el.options.length; i++) {
    el.options[i].selected = selectAll;
  }
  updateAccountSelectBadge(id);
  const count = selectAll ? el.options.length : 0;
  if (selectAll) {
    toast(`Selected all ${count} account(s) ✓`, 'success');
  } else {
    toast('Deselected all accounts', 'info');
  }
}

async function loadVpsStatus() {
  const res = await api('/api/vps/status');
  if (res.error) return;
  set('vps-node-type', res.node_type || 'Unknown');
  set('vps-db-backend', res.database || 'SQLite');
  set('vps-hostname', res.hostname || 'localhost');
  set('vps-os', res.os || '—');
  set('vps-python', res.python_version || '—');
  set('vps-active-camps', `${res.active_campaigns} campaign(s) running 24/7`);

  const cfg = await api('/api/vps/config');
  if (!cfg.error) {
    if (document.getElementById('vps-cfg-url')) set('vps-cfg-url', cfg.vps_url || '');
    if (document.getElementById('vps-cfg-key')) set('vps-cfg-key', cfg.vps_api_key || '');
    const st = document.getElementById('vps-cfg-status');
    if (st) {
      st.textContent = cfg.is_connected ? `🟢 Connected to ${cfg.vps_url}` : '🔴 VPS Not Connected (Local Engine Active)';
      st.style.color = cfg.is_connected ? '#38ef7d' : '#ff4d4d';
    }
    updateExecutionModeDropdown(cfg.is_connected, cfg.vps_url);
  }
}

function updateExecutionModeDropdown() {
  const el = document.getElementById('c-execution-mode');
  if (!el) return;
  el.innerHTML = `
    <option value="vps" selected>🌐 24/7 Cloud Engine (Cloud execution — PC can be closed/turned off)</option>
  `;
}

async function saveVpsConfig() {
  const vps_url = val('vps-cfg-url');
  const vps_api_key = val('vps-cfg-key');
  const res = await api('/api/vps/config', {
    method: 'POST',
    body: JSON.stringify({ vps_url, vps_api_key }),
  });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('VPS Connection updated!', 'success');
  loadVpsStatus();
}

async function api(url, opts = {}) {
  try {
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
      ...opts,
    });
    return await res.json();
  } catch (e) {
    return { error: e.message };
  }
}

function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  const icons = { success: '✓', error: '✕', info: 'ℹ' };
  el.innerHTML = `<span>${icons[type] || 'ℹ'}</span><span>${esc(msg)}</span>`;
  container.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function set(id, v) { const el = document.getElementById(id); if (el) el.value = v; }
function val(id) { const el = document.getElementById(id); return el ? el.value.trim() : ''; }
function esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function fmtDate(s) {
  if (!s) return '—';
  try { return new Date(s).toLocaleString(); } catch (e) { return s; }
}

// ══ Proxy Settings & BetaSocks ═════════════════════════════════════════════════
async function loadProxySettings() {
  const cfg = await api('/api/proxy/settings');
  if (cfg && !cfg.error) {
    if (document.getElementById('px-email')) document.getElementById('px-email').value = cfg.betasocks_email || '';
    if (document.getElementById('px-password')) document.getElementById('px-password').value = cfg.betasocks_password || '';
    if (document.getElementById('px-daily-limit')) document.getElementById('px-daily-limit').value = cfg.daily_limit || 50;
    if (document.getElementById('px-usage-display')) {
      document.getElementById('px-usage-display').textContent = `Fetched ${cfg.fetched_today_count || 0} / ${cfg.daily_limit || 50} proxies today`;
    }
  }
}

async function saveProxySettings() {
  const email = document.getElementById('px-email').value;
  const password = document.getElementById('px-password').value;
  const daily_limit = parseInt(document.getElementById('px-daily-limit').value || 50);

  const res = await api('/api/proxy/settings', {
    method: 'POST',
    body: JSON.stringify({ betasocks_email: email, betasocks_password: password, daily_limit: daily_limit })
  });
  if (res && res.msg) {
    toast(res.msg, 'success');
    loadProxySettings();
  }
}

async function testBetaSocksConnection() {
  const email = document.getElementById('px-email').value;
  const password = document.getElementById('px-password').value;
  toast('Connecting to BetaSocks...', 'info');
  const res = await api('/api/proxy/test', {
    method: 'POST',
    body: JSON.stringify({ betasocks_email: email, betasocks_password: password })
  });
  if (res && res.success) {
    toast('🟢 ' + res.message, 'success');
  } else {
    toast('🔴 ' + (res.message || 'Connection failed'), 'error');
  }
}

async function fetchBetaSocksProxiesNow() {
  toast('Fetching fresh proxies from BetaSocks...', 'info');
  const res = await api('/api/proxy/fetch', {
    method: 'POST',
    body: JSON.stringify({ country: 'usa', limit: 5 })
  });
  if (res && res.proxies && res.proxies.length > 0) {
    toast(`🟢 Successfully retrieved ${res.proxies.length} fresh proxies!`, 'success');
    loadProxySettings();
  } else {
    toast('⚠️ Could not fetch proxies (daily limit reached or account error)', 'warning');
  }
}

async function resetProxyDailyCount() {
  const res = await api('/api/proxy/reset_count', { method: 'POST' });
  if (res && res.msg) {
    toast('🟢 ' + res.msg, 'success');
    loadProxySettings();
  }
}

// ══ Account Creator Submit ═════════════════════════════════════════════════════
async function createAccountSubmit(e) {
  if (e) e.preventDefault();

  const nameInput = document.getElementById('cr-name');
  const name = nameInput ? nameInput.value.trim() : '';
  const quantity = document.getElementById('cr-quantity') ? document.getElementById('cr-quantity').value : '1';
  const description = document.getElementById('cr-bio') ? document.getElementById('cr-bio').value : '';
  const location = document.getElementById('cr-location') ? document.getElementById('cr-location').value : '';
  const url = document.getElementById('cr-url') ? document.getElementById('cr-url').value : '';
  const avatarFile = document.getElementById('cr-avatar') && document.getElementById('cr-avatar').files[0];
  const bannerFile = document.getElementById('cr-banner') && document.getElementById('cr-banner').files[0];

  if (!name) {
    toast('Display Name is required', 'error');
    return;
  }

  const submitBtn = document.querySelector('#form-account-creator button[type="submit"]');
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = '⌛ Creating Account(s)...';
  }

  const formData = new FormData();
  formData.append('name', name);
  formData.append('quantity', quantity);
  if (description) formData.append('description', description);
  if (location) formData.append('location', location);
  if (url) formData.append('url', url);
  if (avatarFile) formData.append('avatar', avatarFile);
  if (bannerFile) formData.append('banner', bannerFile);

  try {
    const resp = await fetch('/api/accounts/create', {
      method: 'POST',
      body: formData
    });
    const res = await resp.json();

    if (res && res.success) {
      toast('🟢 ' + res.message, 'success');
      if (nameInput) nameInput.value = '';
      if (document.getElementById('cr-bio')) document.getElementById('cr-bio').value = '';
      if (document.getElementById('cr-location')) document.getElementById('cr-location').value = '';
      if (document.getElementById('cr-url')) document.getElementById('cr-url').value = '';
      if (document.getElementById('cr-avatar')) document.getElementById('cr-avatar').value = '';
      if (document.getElementById('cr-banner')) document.getElementById('cr-banner').value = '';

      if (typeof loadAccounts === 'function') loadAccounts();
      if (typeof switchTab === 'function') switchTab('accounts');
    } else {
      toast('🔴 ' + (res.error || res.message || 'Account creation failed'), 'error');
    }
  } catch (err) {
    toast('🔴 Network error during account creation: ' + err.message, 'error');
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = '✨ Create & Register Account';
    }
  }
}

// ── BULK IMPORTER & EDITOR MODALS ──────────────────────────────────────────────
function openBulkImportModal() {
  const m = document.getElementById('modal-bulk-import');
  if (m) m.style.display = 'flex';
}

function closeBulkImportModal() {
  const m = document.getElementById('modal-bulk-import');
  if (m) m.style.display = 'none';
}

async function bulkImportSubmit() {
  const txt = (document.getElementById('bulk-import-text')?.value || '').trim();
  if (!txt) {
    toast('Please paste at least one line of auth_token:ct0 credentials', 'error');
    return;
  }
  try {
    const resp = await fetch('/api/accounts/bulk-import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: txt })
    });
    const data = await resp.json();
    if (data.imported > 0) {
      toast(`Successfully imported ${data.imported} account(s)!`, 'success');
      closeBulkImportModal();
      document.getElementById('bulk-import-text').value = '';
      if (typeof loadAccounts === 'function') loadAccounts();
    } else {
      toast('Failed to import accounts: ' + (data.errors ? data.errors.join(', ') : 'Unknown error'), 'error');
    }
  } catch (err) {
    toast('Error importing accounts: ' + err.message, 'error');
  }
}

function openBulkEditModal() {
  const m = document.getElementById('modal-bulk-edit');
  if (m) m.style.display = 'flex';
}

function closeBulkEditModal() {
  const m = document.getElementById('modal-bulk-edit');
  if (m) m.style.display = 'none';
}

async function bulkEditSubmit() {
  const bio = document.getElementById('be-bio')?.value || '';
  const location = document.getElementById('be-location')?.value || '';
  const url = document.getElementById('be-url')?.value || '';

  const submitBtn = document.querySelector("#modal-bulk-edit button.btn-primary");
  const origText = submitBtn ? submitBtn.textContent : 'Update Profiles';
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = 'Updating Profiles...';
  }

  try {
    const resp = await fetch('/api/accounts/bulk-edit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description: bio, location: location, url: url })
    });
    const data = await resp.json();
    if (data.error) {
      toast(data.error, 'error');
    } else {
      toast(data.message || `Successfully started profile update for ${data.total || 0} account(s)!`, 'success');
      closeBulkEditModal();
      if (typeof loadAccounts === 'function') loadAccounts();
    }
  } catch (err) {
    toast('Error updating profiles: ' + err.message, 'error');
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = origText;
    }
  }
}

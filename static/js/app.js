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

  renderCountryChips('c');
  renderCountryChips('edit-c');
  autoConnectActiveCampaign();
});

const TAB_TITLES = {
  accounts: 'Account Manager',
  scraper: 'Twitter Scraper',
  campaign: 'Post Campaign',
  results: 'Results & Files',
  vps: 'VPS & Production Server Manager',
};

function switchTab(tab, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  if (el) el.classList.add('active');
  document.getElementById('tabTitle').textContent = TAB_TITLES[tab] || tab;
  currentTab = tab;

  if (tab === 'accounts') loadAccounts();
  if (tab === 'scraper') { loadAccountSelects(); loadCSVFiles(); }
  if (tab === 'campaign') { loadAccountSelects(); loadCampaignDbPanel(); loadVpsStatus(); }
  if (tab === 'results') { loadJobs(); loadCSVFiles(); }
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
  tbody.innerHTML = list.map(a => `
    <tr>
      <td>${a.id}</td>
      <td>${esc(a.label || '—')}</td>
      <td><code>${esc(a.token_preview)}</code></td>
      <td>${a.proxy ? `<code>${esc(a.proxy)}</code>` : '—'}</td>
      <td>${fmtDate(a.created_at)}</td>
      <td>
        <button class="btn btn-sm btn-ghost" onclick="openEditAccountModal(${a.id})" style="margin-right:4px;">✏️ Edit</button>
        <button class="btn btn-sm btn-danger" onclick="deleteAccount(${a.id})">✕ Remove</button>
      </td>
    </tr>
  `).join('');
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
  c: [],
  'edit-c': [],
};

function addCountryChip(prefix) {
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

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// ══ Scraping ══════════════════════════════════════════════════════════════════
async function startScrape(type) {
  const accountIds = getSelectedAccountIds(type);
  if (!accountIds.length) { toast('Select at least one account', 'error'); return; }

  let payload = { account_ids: accountIds };

  if (type === 'followers') {
    const targets = val('f-targets');
    if (!targets) { toast('Enter at least one target profile', 'error'); return; }
    payload.targets = targets;
    payload.limit = parseInt(val('f-limit') || '100');
    payload.min_followers = parseInt(val('f-min-followers') || '0');
    payload.max_followers = parseInt(val('f-max-followers') || '1000');
    let countryVal = val('f-country');
    if (countryVal === 'custom') countryVal = val('f-custom-country');
    payload.country_filter = countryVal;
  } else if (type === 'search') {
    payload = {
      ...payload,
      query: val('s-query'),
      since: val('s-since'),
      until: val('s-until'),
      from_users: val('s-from-users'),
      lang: val('s-lang'),
      min_likes: val('s-min-likes'),
      min_retweets: val('s-min-rt'),
      has_images: document.getElementById('s-has-images').value === 'true' ? true : null,
      display_type: val('s-display'),
      limit: parseInt(val('s-limit') || '100'),
    };
  } else if (type === 'profile') {
    const targets = val('p-targets');
    if (!targets) { toast('Enter at least one profile', 'error'); return; }
    payload.targets = targets;
    payload.limit = parseInt(val('p-limit') || '100');
  }

  const res = await api(`/api/scrape/${type}`, { method: 'POST', body: JSON.stringify(payload) });
  if (res.error) { toast(res.error, 'error'); return; }

  currentJobId = res.job_id;
  toast(`Job started: ${res.job_id.slice(0, 8)}`, 'success');
  showJobMonitor();
  startJobPoll();
}

function getSelectedAccountIds(prefix) {
  const mapPrefix = { followers: 'f', search: 's', profile: 'p' };
  const p = mapPrefix[prefix] || prefix;
  const el = document.getElementById(`${p}-accounts`);
  if (!el) return accounts.map(a => a.id);
  return Array.from(el.selectedOptions).map(o => parseInt(o.value));
}

function showJobMonitor() {
  const mon = document.getElementById('scraper-job-monitor');
  if (mon) { mon.style.display = 'block'; mon.scrollIntoView({ behavior: 'smooth' }); }
  document.getElementById('job-spinner').className = 'spinner';
  document.getElementById('job-download-btn').style.display = 'none';
  document.getElementById('job-log').innerHTML = '';
}

function startJobPoll() {
  if (jobPollInterval) clearInterval(jobPollInterval);
  jobPollInterval = setInterval(pollJob, 2000);
}

async function pollJob() {
  if (!currentJobId) return;
  const data = await api(`/api/jobs/${currentJobId}`);
  updateJobUI(data);
  if (['done', 'error'].includes(data.status)) {
    clearInterval(jobPollInterval);
    document.getElementById('job-spinner').className = '';
    if (data.status === 'done') {
      document.getElementById('job-download-btn').style.display = 'inline-flex';
      toast('Scrape completed!', 'success');
      loadCSVFiles();
      loadCSVSelect();
    }
  }
}

function updateJobUI(data) {
  const pill = document.getElementById('job-status-pill');
  const txt = document.getElementById('job-status-text');
  if (pill) { pill.textContent = data.status; pill.setAttribute('data-status', data.status); }
  if (txt) txt.textContent = `Job ${data.id?.slice(0, 8)} · ${data.type}`;

  const log = document.getElementById('job-log');
  if (log && data.log) {
    log.innerHTML = data.log.map(e =>
      `<div class="log-entry log-info"><span class="log-ts">${e.ts}</span>${esc(e.msg)}</div>`
    ).join('');
    log.scrollTop = log.scrollHeight;
  }
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
  if (!confirm('Permanently delete ALL scrape jobs and their CSV files?')) return;
  const res = await api('/api/jobs/all', { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast('All scrape jobs deleted', 'info');
  loadJobs();
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
  if (!accountIds.length) { toast('Select at least one posting account in Step 1', 'error'); return; }

  const source_profiles = val('c-source-profiles');
  if (!source_profiles) { toast('Enter at least one source profile to scrape from', 'error'); return; }

  const post_template = val('c-post-template');
  if (!post_template) { toast('Enter a post message template', 'error'); return; }

  const posting_mode = val('c-posting-mode') || 'list_card';
  const display_name = val('c-display-name') || 'Official Notice';
  const body_text    = val('c-body-text') || '';

  if (posting_mode === 'list_card' || posting_mode === 'normal_card') {
    if (!val('c-display-name')) { toast('Display Name is required in Step 1 for Generated Card Image', 'error'); return; }
    if (!val('c-body-text'))    { toast('Tweet Body Text is required in Step 1 for Generated Card Image', 'error'); return; }
  }

  let countryVal = (campaignSelectedCountries['c'] || []).join(', ');
  let normal_media_data = null;

  if (posting_mode === 'normal_custom') {
    const normalMediaInput = document.getElementById('c-normal-media-file');
    if (normalMediaInput && normalMediaInput.files[0]) {
      try {
        normal_media_data = await readFileAsDataURL(normalMediaInput.files[0]);
      } catch (e) {
        toast('Could not read uploaded normal post image', 'error');
      }
    }
  }

  const target_type = val('c-target-type') || 'followers';
  const config = {
    account_ids: accountIds,
    target_type,
    source_profiles,
    display_name,
    username:          val('c-username') || display_name.toLowerCase().replace(/\s+/g, ''),
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
    tags_per_post:     parseInt(document.getElementById('c-tags-per-post').value || '3'),
    min_delay_minutes: parseInt(val('c-min-delay') || '8'),
    max_delay_minutes: parseInt(val('c-max-delay') || '20'),
    max_posts_per_account: parseInt(val('c-max-posts') || '30'),
    min_followers:     parseInt(val('c-min-followers') || '0'),
    max_followers:     parseInt(val('c-max-followers') || '1000'),
    country_filter:    countryVal,
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

  toast('Campaign launched!', 'success');
  document.getElementById('stop-btn').style.display = 'inline-flex';
  document.getElementById('resume-btn').style.display = 'none';
  document.getElementById('clear-tagged-btn').style.display = 'inline-flex';
  updateCampaignStats(config);
  startCampaignPoll();
  loadCampaignDbPanel();
}

function updateCampaignStats(config) {
  document.getElementById('stat-accounts').textContent = (config.account_ids || []).length;
}

async function stopCampaign() {
  if (!currentCampaignId) { toast('No active campaign selected', 'error'); return; }
  await stopCampaignId(currentCampaignId);
}

async function stopCampaignId(cid) {
  await api(`/api/campaigns/${cid}/stop`, { method: 'POST' });
  toast(`Campaign #${cid} stopped`, 'info');
  const stopBtn = document.getElementById('stop-btn');
  const resumeBtn = document.getElementById('resume-btn');
  const pill = document.getElementById('camp-status-pill');
  if (currentCampaignId === cid) {
    if (pill) { pill.textContent = 'stopped'; pill.setAttribute('data-s', 'stopped'); }
    if (stopBtn) stopBtn.style.display = 'none';
    if (resumeBtn) resumeBtn.style.display = 'inline-flex';
  }
  loadCampaignDbPanel();
}

async function resumeCampaign(cid) {
  currentCampaignId = cid;
  const res = await api(`/api/campaigns/${cid}/resume`, { method: 'POST' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`Campaign #${cid} resumed!`, 'success');
  const stopBtn = document.getElementById('stop-btn');
  const resumeBtn = document.getElementById('resume-btn');
  if (stopBtn) stopBtn.style.display = 'inline-flex';
  if (resumeBtn) resumeBtn.style.display = 'none';
  const clearBtn = document.getElementById('clear-tagged-btn');
  if (clearBtn) clearBtn.style.display = 'inline-flex';
  startCampaignPoll();
  loadCampaignDbPanel();
}

async function selectCampaignForLogs(cid) {
  currentCampaignId = cid;
  startCampaignPoll();
  pollCampaign();
  toast(`Viewing logs for Campaign #${cid}`, 'info');
}

async function resumeActiveCampaign() {
  if (currentCampaignId) {
    resumeCampaign(currentCampaignId);
  }
}

function toggleTargetType(val) {
  const lbl = document.getElementById('lbl-target-input');
  const inp = document.getElementById('c-source-profiles');
  const hint = document.getElementById('hint-target-input');
  if (val === 'tweet_commenters') {
    if (lbl) lbl.innerHTML = 'Tweet URL or Tweet ID <span class="badge-required">required</span>';
    if (inp) inp.placeholder = 'https://x.com/elonmusk/status/1820000000000000000';
    if (hint) hint.textContent = 'Paste Tweet URL or ID. Streamingly scrapes users who commented on this tweet and tags them.';
  } else if (val === 'target_tweets_commenters') {
    if (lbl) lbl.innerHTML = 'Target Profile(s) to Scrape Comments From <span class="badge-required">required</span>';
    if (inp) inp.placeholder = 'elonmusk, OpenAI, nasa';
    if (hint) hint.textContent = 'Comma-separated usernames. Scrapes recent top tweets posted by target profiles and extracts their comments first.';
  } else {
    if (lbl) lbl.innerHTML = 'Source Profiles to Scrape From <span class="badge-required">required</span>';
    if (inp) inp.placeholder = 'elonmusk, OpenAI, nasa';
    if (hint) hint.textContent = 'Comma-separated usernames. Streamingly scrapes followers from these profiles without saving CSV files.';
  }
}

function toggleScraperTargetType(val) {
  const lbl = document.getElementById('lbl-scraper-target-input');
  const inp = document.getElementById('f-targets');
  const hint = document.getElementById('hint-scraper-target-input');
  if (val === 'tweet_commenters') {
    if (lbl) lbl.innerHTML = 'Tweet URL or Tweet ID <span class="badge-required">required</span>';
    if (inp) inp.placeholder = 'https://x.com/elonmusk/status/1820000000000000000';
    if (hint) hint.textContent = 'Paste Tweet URL or ID. Streamingly scrapes users who commented on this tweet.';
  } else if (val === 'target_tweets_commenters') {
    if (lbl) lbl.innerHTML = 'Target Profile(s) to Scrape Comments From <span class="badge-required">required</span>';
    if (inp) inp.placeholder = 'elonmusk, OpenAI, nasa';
    if (hint) hint.textContent = 'Comma-separated usernames. Scrapes recent top tweets from these profiles and extracts their comment sections first.';
  } else {
    if (lbl) lbl.innerHTML = 'Target Profile(s) <span class="badge-required">required</span>';
    if (inp) inp.placeholder = 'elonmusk, OpenAI, nasa (comma-separated)';
    if (hint) hint.textContent = 'Comma-separated usernames. Streamingly scrapes followers from these profiles without saving CSV files.';
  }
}

function toggleEditTargetType(val) {
  const lbl = document.getElementById('lbl-edit-target-input');
  const inp = document.getElementById('edit-c-source-profiles');
  const hint = document.getElementById('hint-edit-target-input');
  if (val === 'tweet_commenters') {
    if (lbl) lbl.textContent = 'Tweet URL or Tweet ID';
    if (inp) inp.placeholder = 'https://x.com/elonmusk/status/1820000000000000000';
    if (hint) hint.textContent = 'Paste Tweet URL or ID. Streamingly scrapes users who commented on this tweet and tags them.';
  } else if (val === 'target_tweets_commenters') {
    if (lbl) lbl.textContent = 'Target Profile(s) to Scrape Comments From';
    if (inp) inp.placeholder = 'elonmusk, OpenAI, nasa';
    if (hint) hint.textContent = 'Comma-separated usernames. Scrapes recent top tweets posted by target profiles and extracts their comments first.';
  } else {
    if (lbl) lbl.textContent = 'Source Profile(s) to Scrape From';
    if (inp) inp.placeholder = 'elonmusk, OpenAI, nasa';
    if (hint) hint.textContent = 'Comma-separated usernames. Streamingly scrapes followers from these profiles.';
  }
}

function startCampaignPoll() {
  if (campaignPollInterval) clearInterval(campaignPollInterval);
  pollCampaign();
  campaignPollInterval = setInterval(pollCampaign, 3000);
}

async function pollCampaign() {
  if (!currentCampaignId) return;
  try {
    const data = await api(`/api/campaigns/${currentCampaignId}/log`);
    if (data && !data.error) {
      updateCampaignLog(data);
    }

    // Poll tagged count
    const tc = await api(`/api/campaigns/${currentCampaignId}/tagged-count`);
    if (tc && !tc.error) {
      const el = document.getElementById('stat-tagged');
      if (el) el.textContent = tc.tagged_count;
    }

    const editBtn = document.getElementById('edit-camp-btn');
    if (editBtn) editBtn.style.display = 'inline-flex';

    if (data.status === 'running') {
      document.getElementById('stop-btn').style.display = 'inline-flex';
      document.getElementById('resume-btn').style.display = 'none';
    } else if (['done', 'error', 'stopped'].includes(data.status)) {
      document.getElementById('stop-btn').style.display = 'none';
      document.getElementById('resume-btn').style.display = 'inline-flex';
      document.getElementById('clear-tagged-btn').style.display = 'inline-flex';
    }
  } catch (err) {
    console.warn("Campaign poll failed:", err);
  }
}

function updateCampaignLog(data) {
  const pill = document.getElementById('camp-status-pill');
  if (pill) { pill.textContent = data.status; pill.setAttribute('data-s', data.status); }

  const log = document.getElementById('campaign-log');
  if (!log || !data.log) return;

  const postCount = data.log.filter(e => e.level === 'SUCCESS' && (e.msg.includes('Tweeted:') || e.msg.includes('Posted:'))).length;
  document.getElementById('stat-posts').textContent = postCount || '0';

  const lastEntry = data.log.length ? JSON.stringify(data.log[data.log.length - 1]) : '';
  if (log.dataset.lastEntry === lastEntry) {
    return;
  }
  log.dataset.lastEntry = lastEntry;

  // Smart scroll: check if user is at the bottom before rendering new HTML
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

function copyCampaignLogs() {
  const log = document.getElementById('campaign-log');
  if (!log) return;
  const entries = Array.from(log.querySelectorAll('.log-entry'));
  if (!entries.length) { toast('No logs to copy', 'info'); return; }
  const text = entries.map(el => el.textContent).join('\n');

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      toast('Logs copied to clipboard! 📋', 'success');
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
    if (running) {
      currentCampaignId = running.id;
      const stopBtn = document.getElementById('stop-btn');
      const resumeBtn = document.getElementById('resume-btn');
      const pill = document.getElementById('camp-status-pill');
      if (stopBtn) stopBtn.style.display = 'inline-flex';
      if (resumeBtn) resumeBtn.style.display = 'none';
      if (pill) { pill.textContent = 'running'; pill.setAttribute('data-s', 'running'); }
      if (!campaignPollInterval) {
        startCampaignPoll();
      }
    }
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
          return `
            <tr>
              <td>${c.id}</td>
              <td>${esc(c.name)}</td>
              <td><span style="font-size:11px;padding:2px 8px;border-radius:4px;background:rgba(29,155,240,0.15);color:#1d9bf0;font-weight:600;">${modeLabel}</span></td>
              <td><span class="status-pill" data-status="${c.status}">${c.status}</span></td>
              <td><strong>${c.tagged_count.toLocaleString()}</strong></td>
              <td style="display:flex;gap:4px;flex-wrap:wrap;">
                <button class="btn btn-sm btn-ghost" onclick="openEditCampaignModal(${c.id})">✏ Edit</button>
                <button class="btn btn-sm btn-success" onclick="resumeCampaign(${c.id})">▶ Resume</button>
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
    const selectedIds = cfg.account_ids || [];
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
  const account_ids = accSel ? Array.from(accSel.selectedOptions).map(o => parseInt(o.value)) : [];
  if (!account_ids.length) { toast('Select at least one posting account', 'error'); return; }

  // Fetch current config to merge
  const current = await api(`/api/campaigns/${editingCampaignId}`);
  const prevConfig = current.config || {};

  const editCountryVal = (campaignSelectedCountries['edit-c'] || []).join(', ');

  const updatedConfig = {
    ...prevConfig,
    account_ids,
    posting_mode: val('edit-c-posting-mode') || 'list_card',
    target_type: val('edit-c-target-type') || 'followers',
    source_profiles: val('edit-c-source-profiles'),
    min_followers: parseInt(val('edit-c-min-followers') || '0'),
    max_followers: parseInt(val('edit-c-max-followers') || '1000'),
    country_filter: editCountryVal,
    post_template: val('edit-c-post-template'),
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
  if (currentCampaignId) {
    currentCampaignId = null;
    clearInterval(campaignPollInterval);
    document.getElementById('stop-btn').style.display = 'none';
    document.getElementById('clear-tagged-btn').style.display = 'none';
    document.getElementById('stat-tagged').textContent = '--';
    document.getElementById('stat-posts').textContent = '--';
    document.getElementById('camp-status-pill').textContent = 'idle';
    document.getElementById('camp-status-pill').setAttribute('data-s', 'idle');
    document.getElementById('campaign-log').innerHTML = '<div class="log-entry log-info">Waiting for campaign to start...</div>';
  }
  loadCampaignDbPanel();
}

async function deleteCampaign(cid) {
  if (!confirm(`Permanently delete campaign #${cid} and all its tagged history?`)) return;
  const res = await api(`/api/campaigns/${cid}`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`Campaign #${cid} deleted`, 'info');
  if (currentCampaignId === cid) {
    currentCampaignId = null;
    clearInterval(campaignPollInterval);
    document.getElementById('stop-btn').style.display = 'none';
    document.getElementById('clear-tagged-btn').style.display = 'none';
    document.getElementById('stat-tagged').textContent = '--';
    document.getElementById('stat-posts').textContent = '--';
    document.getElementById('camp-status-pill').textContent = 'idle';
    document.getElementById('camp-status-pill').setAttribute('data-s', 'idle');
    document.getElementById('campaign-log').innerHTML = '<div class="log-entry log-info">Waiting for campaign to start...</div>';
  }
  loadCampaignDbPanel();
}

async function clearCampaignTagged(cid) {
  if (!confirm(`Clear tagged history for campaign #${cid}?`)) return;
  const res = await api(`/api/campaigns/${cid}/tagged`, { method: 'DELETE' });
  if (res.error) { toast(res.error, 'error'); return; }
  toast(`Cleared tagged users for campaign #${cid}`, 'success');
  if (currentCampaignId === cid) document.getElementById('stat-tagged').textContent = '0';
  loadCampaignDbPanel();
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

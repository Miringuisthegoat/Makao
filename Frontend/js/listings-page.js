/* =============================================
   Makao — listings-page.js
   All interactivity for listings.html:
   filters, sort, grid/list toggle, pagination,
   URL state, skeleton loading, active filter tags
   ============================================= */

const API_BASE = '/api';
const PAGE_SIZE = 12;

/* ── State ── */
let state = {
  page: 1,
  sort: 'newest',
  view: 'grid',          // 'grid' | 'list'
  query: '',
  counties: [],
  minPrice: '',
  maxPrice: '',
  bedrooms: '',
  bathrooms: '',
  types: [],
  amenities: [],
  hostTypes: [],
  availability: [],
};

/* ── DOM refs ── */
const gridEl       = document.getElementById('listingsGrid');
const listEl       = document.getElementById('listingsList');
const noResults    = document.getElementById('noResults');
const paginationEl = document.getElementById('pagination');
const countEl      = document.getElementById('resultsCount');
const activeTagsEl = document.getElementById('activeFilters');
const filterBadge  = document.getElementById('filterCountBadge');

/* ===== INIT ===== */
function init() {
  readURLParams();
  bindEvents();
  applyStateToUI();
  fetchListings();
}

/* ===== READ URL PARAMS ===== */
function readURLParams() {
  const p = new URLSearchParams(window.location.search);
  if (p.get('q'))        state.query    = p.get('q');
  if (p.get('county'))   state.counties = [p.get('county')];
  if (p.get('beds'))     state.bedrooms = p.get('beds');
  if (p.get('filter')) {
    const f = p.get('filter');
    if (['nairobi','mombasa','kisumu','nakuru','eldoret'].includes(f)) state.counties = [f];
    if (f === 'furnished') state.amenities = ['furnished'];
    if (f === 'wifi')      state.amenities = ['wifi'];
  }
  if (p.get('budget')) {
    const [mn, mx] = (p.get('budget') || '').split('-');
    state.minPrice = mn || '';
    state.maxPrice = mx || '';
  }
  if (p.get('sort'))     state.sort = p.get('sort');
  if (p.get('page'))     state.page = parseInt(p.get('page'), 10) || 1;
}

/* ===== APPLY STATE TO UI (sync checkboxes, pills, inputs) ===== */
function applyStateToUI() {
  // Search input
  const srch = document.getElementById('inlineSearch');
  if (srch) srch.value = state.query;

  // Counties
  document.querySelectorAll('[name="county"]').forEach(cb => {
    cb.checked = state.counties.includes(cb.value);
  });

  // Budget
  const minP = document.getElementById('minPrice');
  const maxP = document.getElementById('maxPrice');
  if (minP) minP.value = state.minPrice;
  if (maxP) maxP.value = state.maxPrice;

  // Bedrooms pills
  setActivePill('bedroomPills', state.bedrooms);

  // Bathrooms pills
  setActivePill('bathroomPills', state.bathrooms);

  // Sort
  const sortSel = document.getElementById('sortSelect');
  if (sortSel) sortSel.value = state.sort;

  // Types & amenities
  document.querySelectorAll('[name="type"]').forEach(cb => {
    cb.checked = state.types.includes(cb.value);
  });
  document.querySelectorAll('[name="amenity"]').forEach(cb => {
    cb.checked = state.amenities.includes(cb.value);
  });
  document.querySelectorAll('[name="host_type"]').forEach(cb => {
    cb.checked = state.hostTypes.includes(cb.value);
  });

  // Budget presets
  document.querySelectorAll('.preset-btn').forEach(btn => {
    const mn = btn.dataset.min;
    const mx = btn.dataset.max;
    btn.classList.toggle('active', mn === state.minPrice && mx === state.maxPrice);
  });

  renderActiveTags();
  updateFilterBadge();
}

function setActivePill(containerId, val) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.querySelectorAll('.pill-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.val === val);
  });
}

/* ===== BIND EVENTS ===== */
function bindEvents() {

  // Inline search
  const srch = document.getElementById('inlineSearch');
  const clrBtn = document.getElementById('inlineSearchClear');
  if (srch) {
    srch.addEventListener('input', () => {
      state.query = srch.value.trim();
      clrBtn?.classList.toggle('visible', state.query.length > 0);
    });
    srch.addEventListener('keydown', e => {
      if (e.key === 'Enter') { state.page = 1; fetchListings(); }
    });
  }
  clrBtn?.addEventListener('click', () => {
    state.query = '';
    if (srch) srch.value = '';
    clrBtn.classList.remove('visible');
    fetchListings();
  });

  // Sort
  document.getElementById('sortSelect')?.addEventListener('change', e => {
    state.sort = e.target.value;
    state.page = 1;
    fetchListings();
  });

  // Grid / List toggle
  document.getElementById('gridViewBtn')?.addEventListener('click', () => switchView('grid'));
  document.getElementById('listViewBtn')?.addEventListener('click', () => switchView('list'));

  // Filter sidebar toggle (mobile)
  const filterToggleBtn = document.getElementById('filterToggleBtn');
  const sidebar         = document.getElementById('filtersSidebar');
  const overlay         = document.getElementById('mobileFilterOverlay');
  filterToggleBtn?.addEventListener('click', () => {
    const isOpen = sidebar?.classList.contains('open');
    sidebar?.classList.toggle('open', !isOpen);
    overlay?.classList.toggle('visible', !isOpen);
    filterToggleBtn?.classList.toggle('active', !isOpen);
    filterToggleBtn?.setAttribute('aria-expanded', String(!isOpen));
  });
  overlay?.addEventListener('click', () => {
    sidebar?.classList.remove('open');
    overlay.classList.remove('visible');
    filterToggleBtn?.classList.remove('active');
  });

  // Counties
  document.querySelectorAll('[name="county"]').forEach(cb => {
    cb.addEventListener('change', () => {
      state.counties = Array.from(document.querySelectorAll('[name="county"]:checked'))
        .map(el => el.value);
    });
  });

  // County search filter
  document.getElementById('countySearch')?.addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('#countyList .filter-check').forEach(row => {
      row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });

  // Budget inputs
  document.getElementById('minPrice')?.addEventListener('change', e => { state.minPrice = e.target.value; });
  document.getElementById('maxPrice')?.addEventListener('change', e => { state.maxPrice = e.target.value; });

  // Budget presets
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.minPrice = btn.dataset.min;
      state.maxPrice = btn.dataset.max;
      const minP = document.getElementById('minPrice');
      const maxP = document.getElementById('maxPrice');
      if (minP) minP.value = state.minPrice;
      if (maxP) maxP.value = state.maxPrice || '';
    });
  });

  // Bedroom pills
  document.getElementById('bedroomPills')?.addEventListener('click', e => {
    const btn = e.target.closest('.pill-btn');
    if (!btn) return;
    state.bedrooms = btn.dataset.val;
    setActivePill('bedroomPills', state.bedrooms);
  });

  // Bathroom pills
  document.getElementById('bathroomPills')?.addEventListener('click', e => {
    const btn = e.target.closest('.pill-btn');
    if (!btn) return;
    state.bathrooms = btn.dataset.val;
    setActivePill('bathroomPills', state.bathrooms);
  });

  // Property types
  document.querySelectorAll('[name="type"]').forEach(cb => {
    cb.addEventListener('change', () => {
      state.types = Array.from(document.querySelectorAll('[name="type"]:checked')).map(el => el.value);
    });
  });

  // Amenities
  document.querySelectorAll('[name="amenity"]').forEach(cb => {
    cb.addEventListener('change', () => {
      state.amenities = Array.from(document.querySelectorAll('[name="amenity"]:checked')).map(el => el.value);
    });
  });

  // Host types
  document.querySelectorAll('[name="host_type"]').forEach(cb => {
    cb.addEventListener('change', () => {
      state.hostTypes = Array.from(document.querySelectorAll('[name="host_type"]:checked')).map(el => el.value);
    });
  });

  // Availability
  document.querySelectorAll('[name="availability"]').forEach(cb => {
    cb.addEventListener('change', () => {
      state.availability = Array.from(document.querySelectorAll('[name="availability"]:checked')).map(el => el.value);
    });
  });

  // Apply filters button
  document.getElementById('applyFiltersBtn')?.addEventListener('click', () => {
    state.page = 1;
    renderActiveTags();
    updateFilterBadge();
    fetchListings();
    // Close sidebar on mobile
    document.getElementById('filtersSidebar')?.classList.remove('open');
    document.getElementById('mobileFilterOverlay')?.classList.remove('visible');
    document.getElementById('filterToggleBtn')?.classList.remove('active');
  });

  // Clear all filters
  document.getElementById('clearAllBtn')?.addEventListener('click', clearAllFilters);
}

/* ===== SWITCH VIEW ===== */
function switchView(mode) {
  state.view = mode;
  const gridBtn = document.getElementById('gridViewBtn');
  const listBtn = document.getElementById('listViewBtn');
  gridBtn?.classList.toggle('active', mode === 'grid');
  listBtn?.classList.toggle('active', mode === 'list');
  if (gridEl) gridEl.style.display = mode === 'grid' ? '' : 'none';
  if (listEl) listEl.style.display = mode === 'list' ? '' : 'none';
}

/* ===== CLEAR ALL FILTERS ===== */
function clearAllFilters() {
  state = { ...state, counties: [], minPrice: '', maxPrice: '', bedrooms: '', bathrooms: '',
    types: [], amenities: [], hostTypes: [], availability: [], query: '', page: 1 };
  applyStateToUI();
  fetchListings();
}
window.clearAllFilters = clearAllFilters;

/* ===== ACTIVE FILTER TAGS ===== */
const TAG_LABELS = {
  county:       v => v.charAt(0).toUpperCase() + v.slice(1),
  minPrice:     v => `From KES ${Number(v).toLocaleString('en-KE')}`,
  maxPrice:     v => `Up to KES ${Number(v).toLocaleString('en-KE')}`,
  bedrooms:     v => v === '0' ? 'Studio' : `${v} Bed${v > 1 ? 's' : ''}`,
  bathrooms:    v => `${v} Bath${v > 1 ? 's' : ''}`,
  type:         v => v.charAt(0).toUpperCase() + v.slice(1),
  amenity:      v => v.charAt(0).toUpperCase() + v.slice(1).replace(/_/g, ' '),
  host_type:    v => v === 'agency' ? 'Verified Agencies' : 'Landlords',
  availability: v => v === 'now' ? 'Available now' : 'Available this month',
};

function renderActiveTags() {
  if (!activeTagsEl) return;
  const tags = [];

  state.counties.forEach(v     => tags.push({ key: 'county',   val: v, label: TAG_LABELS.county(v) }));
  if (state.minPrice)    tags.push({ key: 'minPrice',    val: '', label: TAG_LABELS.minPrice(state.minPrice) });
  if (state.maxPrice)    tags.push({ key: 'maxPrice',    val: '', label: TAG_LABELS.maxPrice(state.maxPrice) });
  if (state.bedrooms)    tags.push({ key: 'bedrooms',    val: '', label: TAG_LABELS.bedrooms(state.bedrooms) });
  if (state.bathrooms)   tags.push({ key: 'bathrooms',   val: '', label: TAG_LABELS.bathrooms(state.bathrooms) });
  state.types.forEach(v        => tags.push({ key: 'type',      val: v, label: TAG_LABELS.type(v) }));
  state.amenities.forEach(v    => tags.push({ key: 'amenity',   val: v, label: TAG_LABELS.amenity(v) }));
  state.hostTypes.forEach(v    => tags.push({ key: 'host_type', val: v, label: TAG_LABELS.host_type(v) }));
  state.availability.forEach(v => tags.push({ key: 'availability', val: v, label: TAG_LABELS.availability(v) }));
  if (state.query)       tags.push({ key: 'query', val: '', label: `"${state.query}"` });

  activeTagsEl.innerHTML = tags.map(t => `
    <span class="filter-tag">
      ${t.label}
      <button onclick="removeTag('${t.key}','${t.val}')" aria-label="Remove filter">
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </span>`).join('');
}

function removeTag(key, val) {
  switch (key) {
    case 'county':       state.counties     = state.counties.filter(v => v !== val); break;
    case 'minPrice':     state.minPrice     = ''; break;
    case 'maxPrice':     state.maxPrice     = ''; break;
    case 'bedrooms':     state.bedrooms     = ''; break;
    case 'bathrooms':    state.bathrooms    = ''; break;
    case 'type':         state.types        = state.types.filter(v => v !== val); break;
    case 'amenity':      state.amenities    = state.amenities.filter(v => v !== val); break;
    case 'host_type':    state.hostTypes    = state.hostTypes.filter(v => v !== val); break;
    case 'availability': state.availability = state.availability.filter(v => v !== val); break;
    case 'query':        state.query        = ''; break;
  }
  state.page = 1;
  applyStateToUI();
  fetchListings();
}
window.removeTag = removeTag;

/* ===== FILTER BADGE COUNT ===== */
function updateFilterBadge() {
  const count =
    state.counties.length +
    (state.minPrice ? 1 : 0) +
    (state.maxPrice ? 1 : 0) +
    (state.bedrooms ? 1 : 0) +
    (state.bathrooms ? 1 : 0) +
    state.types.length +
    state.amenities.length +
    state.hostTypes.length +
    state.availability.length;

  if (filterBadge) {
    filterBadge.textContent = count;
    filterBadge.style.display = count > 0 ? 'flex' : 'none';
  }
}

/* ===== BUILD QUERY STRING ===== */
function buildParams() {
  const p = new URLSearchParams();
  p.set('page', state.page);
  p.set('limit', PAGE_SIZE);
  p.set('sort', state.sort);
  if (state.query)              p.set('q', state.query);
  if (state.counties.length)    p.set('county', state.counties.join(','));
  if (state.minPrice)           p.set('min_price', state.minPrice);
  if (state.maxPrice)           p.set('max_price', state.maxPrice);
  if (state.bedrooms)           p.set('bedrooms', state.bedrooms);
  if (state.bathrooms)          p.set('bathrooms', state.bathrooms);
  if (state.types.length)       p.set('type', state.types.join(','));
  if (state.amenities.length)   p.set('amenities', state.amenities.join(','));
  if (state.hostTypes.length)   p.set('host_type', state.hostTypes.join(','));
  if (state.availability.length) p.set('availability', state.availability.join(','));
  return p;
}

/* ===== FETCH LISTINGS ===== */
async function fetchListings() {
  showSkeletons();
  const params = buildParams();

  // Update URL without reload
  history.replaceState(null, '', `?${params.toString()}`);

  try {
    const res  = await fetch(`${API_BASE}/listings?${params.toString()}`);
    if (!res.ok) throw new Error('API error');
    const data = await res.json();
    renderListings(data.listings || [], data.total || 0);
    updateCountyCounts(data.county_counts || {});
  } catch (err) {
    // Dev mode — render demo cards
    renderListings(getDemoListings(), 24);
  }
}

/* ===== RENDER LISTINGS ===== */
function renderListings(listings, total) {
  // Update result count
  if (countEl) {
    const from = (state.page - 1) * PAGE_SIZE + 1;
    const to   = Math.min(state.page * PAGE_SIZE, total);
    countEl.textContent = total > 0
      ? `Showing ${from}–${to} of ${total.toLocaleString('en-KE')} listings`
      : '0 listings found';
  }

  if (listings.length === 0) {
    if (gridEl) gridEl.innerHTML = '';
    if (listEl) listEl.innerHTML = '';
    if (noResults) noResults.style.display = 'flex';
    if (paginationEl) paginationEl.innerHTML = '';
    return;
  }

  if (noResults) noResults.style.display = 'none';

  // Grid view
  if (gridEl) gridEl.innerHTML = listings.map(renderGridCard).join('');
  // List view
  if (listEl) listEl.innerHTML = listings.map(renderListRow).join('');

  renderPagination(total);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ===== GRID CARD ===== */
function renderGridCard(l) {
  const price   = l.price_per_month ? `KES ${Number(l.price_per_month).toLocaleString('en-KE')}` : 'Price on request';
  const badge   = getBadge(l);
  const photo   = l.photos?.[0] ? `<img src="${l.photos[0]}" alt="${l.title}" loading="lazy" />` : '';
  const photoBg = !l.photos?.[0] ? `style="background:${randomGradient(l.id)}"` : '';

  return `
    <div class="listing-card" onclick="window.location.href='listing.html?id=${l.id}'">
      <div class="card-photo" ${photoBg}>
        ${photo}
        ${badge}
        <div class="card-heart" onclick="event.stopPropagation(); toggleSave(${l.id}, this)">♡</div>
      </div>
      <div class="card-body">
        <div class="card-price">${price} <span>/ month</span></div>
        <div class="card-title">${escHtml(l.title)}</div>
        <div class="card-location">
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
          ${escHtml(l.location || l.city || '')}${l.county ? ', ' + l.county : ''}
        </div>
        <div class="card-meta">
          <div class="card-meta-item">🛏 ${l.bedrooms ?? 'Studio'}</div>
          <div class="card-meta-item">🚿 ${l.bathrooms ?? 1}</div>
          ${l.size_sqft ? `<div class="card-meta-item">📐 ${l.size_sqft} sqft</div>` : ''}
        </div>
      </div>
    </div>`;
}

/* ===== LIST ROW ===== */
function renderListRow(l) {
  const price = l.price_per_month ? `KES ${Number(l.price_per_month).toLocaleString('en-KE')}` : 'Price on request';
  const badge = getBadge(l);
  const photo = l.photos?.[0]
    ? `<img src="${l.photos[0]}" alt="${l.title}" loading="lazy" />`
    : `<div style="width:100%;height:100%;background:${randomGradient(l.id)}"></div>`;

  return `
    <div class="listing-row" onclick="window.location.href='listing.html?id=${l.id}'">
      <div class="row-photo">${photo}${badge}</div>
      <div class="row-body">
        <div class="row-price">${price} <span>/ month</span></div>
        <div class="row-title">${escHtml(l.title)}</div>
        <div class="row-location">
          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
          ${escHtml(l.location || l.city || '')}${l.county ? ', ' + l.county : ''}
        </div>
        <div class="row-meta">
          <span>🛏 ${l.bedrooms ?? 'Studio'} bed</span>
          <span>🚿 ${l.bathrooms ?? 1} bath</span>
          ${l.size_sqft ? `<span>📐 ${l.size_sqft} sqft</span>` : ''}
        </div>
        ${l.description ? `<div class="row-desc">${escHtml(l.description)}</div>` : ''}
      </div>
      <div class="row-actions">
        <div class="row-heart" onclick="event.stopPropagation(); toggleSave(${l.id}, this)">♡</div>
        <button class="row-view-btn" onclick="event.stopPropagation(); window.location.href='listing.html?id=${l.id}'">View →</button>
      </div>
    </div>`;
}

/* ===== BADGE HELPER ===== */
function getBadge(l) {
  if (l.package_type === 'agency')
    return `<span class="card-badge verified">✓ Verified Agency</span>`;
  if (l.is_featured)
    return `<span class="card-badge featured">⭐ Featured</span>`;
  return `<span class="card-badge">New</span>`;
}

/* ===== PAGINATION ===== */
function renderPagination(total) {
  if (!paginationEl) return;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  if (totalPages <= 1) { paginationEl.innerHTML = ''; return; }

  let html = '';
  const cur = state.page;

  // Prev
  html += `<button class="page-btn" onclick="goPage(${cur - 1})" ${cur === 1 ? 'disabled' : ''}>‹ Prev</button>`;

  // Page numbers with ellipsis
  const pages = buildPageRange(cur, totalPages);
  pages.forEach(p => {
    if (p === '...') {
      html += `<span class="page-dots">…</span>`;
    } else {
      html += `<button class="page-btn ${p === cur ? 'active' : ''}" onclick="goPage(${p})">${p}</button>`;
    }
  });

  // Next
  html += `<button class="page-btn" onclick="goPage(${cur + 1})" ${cur === totalPages ? 'disabled' : ''}>Next ›</button>`;
  paginationEl.innerHTML = html;
}

function buildPageRange(cur, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  if (cur <= 4)  return [1, 2, 3, 4, 5, '...', total];
  if (cur >= total - 3) return [1, '...', total-4, total-3, total-2, total-1, total];
  return [1, '...', cur-1, cur, cur+1, '...', total];
}

function goPage(p) {
  state.page = p;
  fetchListings();
}
window.goPage = goPage;

/* ===== SKELETON LOADING ===== */
function showSkeletons() {
  if (noResults) noResults.style.display = 'none';
  const skeletonCard = `
    <div class="listing-card skeleton-card">
      <div class="card-photo skeleton-photo"></div>
      <div class="card-body">
        <div class="skeleton-line long"></div>
        <div class="skeleton-line short"></div>
        <div class="skeleton-line mid"></div>
      </div>
    </div>`;
  if (gridEl && state.view === 'grid') gridEl.innerHTML = skeletonCard.repeat(PAGE_SIZE);
  if (listEl && state.view === 'list') listEl.innerHTML = skeletonCard.repeat(PAGE_SIZE);
}

/* ===== COUNTY COUNTS ===== */
function updateCountyCounts(counts) {
  document.querySelectorAll('[name="county"]').forEach(cb => {
    const row   = cb.closest('.filter-check');
    const badge = row?.querySelector('.check-count');
    if (badge) badge.textContent = (counts[cb.value] || 0).toLocaleString('en-KE');
  });
}

/* ===== SAVE / HEART ===== */
function toggleSave(id, el) {
  const saved = el.textContent === '♥';
  el.textContent = saved ? '♡' : '♥';
  el.style.color  = saved ? '' : '#c8522a';
  // TODO: persist to localStorage or API when user is logged in
}
window.toggleSave = toggleSave;

/* ===== HELPERS ===== */
function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

const GRADIENTS = [
  'linear-gradient(135deg,#1a3a1a,#2d5a27)',
  'linear-gradient(135deg,#1a2a4a,#2a4a7a)',
  'linear-gradient(135deg,#2a1a3a,#4a2a6a)',
  'linear-gradient(135deg,#3d2b1f,#6b4226)',
  'linear-gradient(135deg,#0f3443,#136a8a)',
  'linear-gradient(135deg,#1f1f3d,#2b2b6b)',
  'linear-gradient(135deg,#2d1a1a,#5a2a2a)',
  'linear-gradient(135deg,#1a2a2a,#2a4a4a)',
];
function randomGradient(id) { return GRADIENTS[id % GRADIENTS.length]; }

/* ===== DEV DEMO DATA ===== */
function getDemoListings() {
  const listings = [
    { id:1,  title:'Modern 2-Bedroom Apartment',     price_per_month:45000, city:'Nairobi', county:'Nairobi', location:'Kilimani',    bedrooms:2, bathrooms:2, package_type:'agency',    is_featured:true  },
    { id:2,  title:'Cozy Bedsitter with WiFi',        price_per_month:14000, city:'Nairobi', county:'Nairobi', location:'Westlands',   bedrooms:0, bathrooms:1, package_type:'landlord',  is_featured:false },
    { id:3,  title:'Spacious 3-Bed Bungalow',         price_per_month:85000, city:'Nairobi', county:'Nairobi', location:'Lavington',   bedrooms:3, bathrooms:3, package_type:'agency',    is_featured:true  },
    { id:4,  title:'1-Bed Studio, South B',           price_per_month:20000, city:'Nairobi', county:'Nairobi', location:'South B',     bedrooms:1, bathrooms:1, package_type:'landlord',  is_featured:false },
    { id:5,  title:'Executive 2-Bed, Upperhill',      price_per_month:58000, city:'Nairobi', county:'Nairobi', location:'Upperhill',   bedrooms:2, bathrooms:2, package_type:'agency',    is_featured:true  },
    { id:6,  title:'2-Bed Apartment near CBD',        price_per_month:32000, city:'Nairobi', county:'Nairobi', location:'Ngara',       bedrooms:2, bathrooms:1, package_type:'landlord',  is_featured:false },
    { id:7,  title:'3-Bed Maisonette, Karen',         price_per_month:95000, city:'Nairobi', county:'Nairobi', location:'Karen',       bedrooms:3, bathrooms:3, package_type:'agency',    is_featured:true  },
    { id:8,  title:'Furnished Bedsitter, Buruburu',   price_per_month:12000, city:'Nairobi', county:'Nairobi', location:'Buruburu',    bedrooms:0, bathrooms:1, package_type:'landlord',  is_featured:false },
    { id:9,  title:'2-Bed Mombasa Road View',         price_per_month:28000, city:'Nairobi', county:'Nairobi', location:'Embakasi',    bedrooms:2, bathrooms:1, package_type:'landlord',  is_featured:false },
    { id:10, title:'Luxury 4-Bed Villa, Runda',       price_per_month:250000,city:'Nairobi', county:'Nairobi', location:'Runda',       bedrooms:4, bathrooms:4, package_type:'agency',    is_featured:true  },
    { id:11, title:'1-Bed Apartment, Ruaka',          price_per_month:22000, city:'Nairobi', county:'Nairobi', location:'Ruaka',       bedrooms:1, bathrooms:1, package_type:'landlord',  is_featured:false },
    { id:12, title:'3-Bed Townhouse, Syokimau',       price_per_month:65000, city:'Nairobi', county:'Nairobi', location:'Syokimau',    bedrooms:3, bathrooms:2, package_type:'agency',    is_featured:false },
  ];
  return listings;
}

/* ===== START ===== */
init();
/* =============================================
   Makao — listings.js
   Loads featured listings on homepage
   ============================================= */

const API_BASE = '/api';

// ===== RENDER LISTING CARD =====
function renderCard(listing) {
  const price = listing.price_per_month
    ? `KES ${Number(listing.price_per_month).toLocaleString('en-KE')}`
    : 'Price on request';

  const badge = listing.package_type === 'agency'
    ? `<span class="card-badge verified">✓ Verified Agency</span>`
    : listing.is_featured
    ? `<span class="card-badge featured">⭐ Featured</span>`
    : '';

  const photo = listing.photos?.[0]
    ? listing.photos[0]
    : 'images/placeholder.jpg';

  return `
    <div class="listing-card" onclick="window.location.href='listing.html?id=${listing.id}'">
      <div class="card-photo">
        <img src="${photo}" alt="${listing.title}" loading="lazy" />
        ${badge}
        <div class="card-heart">♡</div>
      </div>
      <div class="card-body">
        <div class="card-price">${price} <span>/ month</span></div>
        <div class="card-title">${listing.title}</div>
        <div class="card-location">
          <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
          ${listing.location || listing.city}${listing.county ? ', ' + listing.county : ''}
        </div>
        <div class="card-meta">
          <div class="card-meta-item">
            🛏 ${listing.bedrooms ?? 'Studio'}
          </div>
          <div class="card-meta-item">
            🚿 ${listing.bathrooms ?? 1} bath
          </div>
          ${listing.size_sqft ? `<div class="card-meta-item">📐 ${listing.size_sqft} sqft</div>` : ''}
        </div>
      </div>
    </div>`;
}

// ===== LOAD FEATURED LISTINGS =====
async function loadFeaturedListings() {
  const grid = document.getElementById('featuredListings');
  if (!grid) return;

  try {
    const res  = await fetch(`${API_BASE}/listings?featured=true&limit=6`);
    if (!res.ok) throw new Error('API error');
    const data = await res.json();

    if (!data.listings || data.listings.length === 0) {
      grid.innerHTML = `
        <div style="grid-column:1/-1; text-align:center; padding:60px 0; color:#888;">
          <p style="font-size:32px; margin-bottom:12px;">🏠</p>
          <p>No featured listings yet. Be the first to <a href="signup.html" style="color:#c8522a;">advertise your property!</a></p>
        </div>`;
      return;
    }

    grid.innerHTML = data.listings.map(renderCard).join('');
  } catch (err) {
    // Show placeholder cards on error (dev mode)
    grid.innerHTML = Array(3).fill(0).map((_, i) => renderCard({
      id: i + 1,
      title: ['Modern 2-Bedroom in Kilimani', 'Cozy Bedsitter in Westlands', 'Spacious 3-Bed in Lavington'][i],
      price_per_month: [45000, 18000, 85000][i],
      location: ['Kilimani', 'Westlands', 'Lavington'][i],
      city: 'Nairobi',
      county: 'Nairobi',
      bedrooms: [2, 'Studio', 3][i],
      bathrooms: [2, 1, 3][i],
      package_type: ['agency', 'landlord', 'agency'][i],
      is_featured: true,
      photos: []
    })).join('');
  }
}

// Run on page load
loadFeaturedListings();
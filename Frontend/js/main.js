/* =============================================
   Makao — main.js
   General page interactivity
   ============================================= */

// ===== NAVBAR SCROLL EFFECT =====
const navbar = document.getElementById('navbar');
if (navbar) {
  window.addEventListener('scroll', () => {
    navbar.classList.toggle('scrolled', window.scrollY > 40);
  }, { passive: true });
}

// ===== HAMBURGER MENU =====
const hamburger = document.getElementById('hamburger');
const mobileMenu = document.getElementById('mobileMenu');
if (hamburger && mobileMenu) {
  hamburger.addEventListener('click', () => {
    mobileMenu.classList.toggle('open');
    hamburger.setAttribute('aria-expanded', mobileMenu.classList.contains('open'));
  });
  // close on outside click
  document.addEventListener('click', (e) => {
    if (!hamburger.contains(e.target) && !mobileMenu.contains(e.target)) {
      mobileMenu.classList.remove('open');
    }
  });
}

// ===== HERO IMAGE LOAD ANIMATION =====
const heroImg = document.getElementById('heroImg');
if (heroImg) {
  heroImg.addEventListener('load', () => heroImg.classList.add('loaded'));
  if (heroImg.complete) heroImg.classList.add('loaded');
}

// ===== SEARCH REDIRECT =====
const searchBtn = document.getElementById('searchBtn');
if (searchBtn) {
  searchBtn.addEventListener('click', () => {
    const query   = document.getElementById('searchInput')?.value.trim() || '';
    const beds    = document.getElementById('bedroomsSelect')?.value || '';
    const budget  = document.getElementById('budgetSelect')?.value || '';
    const params  = new URLSearchParams();
    if (query)  params.set('q', query);
    if (beds)   params.set('beds', beds);
    if (budget) params.set('budget', budget);
    window.location.href = `listings.html?${params.toString()}`;
  });
  // Enter key
  document.getElementById('searchInput')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchBtn.click();
  });
}

// ===== QUICK FILTER CHIPS =====
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    chip.classList.toggle('active');
    const filter = chip.dataset.filter;
    // redirect to listings with filter applied
    window.location.href = `listings.html?filter=${encodeURIComponent(filter)}`;
  });
});

// ===== ANIMATED COUNTER =====
function animateCounter(el) {
  const target = parseInt(el.dataset.target, 10);
  const duration = 1800;
  const step = Math.ceil(target / (duration / 16));
  let current = 0;
  const timer = setInterval(() => {
    current = Math.min(current + step, target);
    el.textContent = current.toLocaleString('en-KE');
    if (current >= target) clearInterval(timer);
  }, 16);
}

// Trigger counters when stats band enters viewport
const statNums = document.querySelectorAll('.stat-num');
if (statNums.length) {
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        animateCounter(entry.target);
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.5 });
  statNums.forEach(el => observer.observe(el));
}

// ===== COUNTY LISTING COUNTS =====
// Fetches counts from API when page loads
async function loadCountyCounts() {
  try {
    const res = await fetch('/api/listings/county-counts');
    if (!res.ok) return;
    const data = await res.json();
    document.querySelectorAll('[data-county]').forEach(el => {
      const county = el.dataset.county;
      const count  = data[county] ?? 0;
      el.textContent = `${count} listing${count !== 1 ? 's' : ''}`;
    });
  } catch (_) {
    // silently fail — counts just won't update
  }
}
loadCountyCounts();
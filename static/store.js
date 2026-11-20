/* Moved out of store.html (Phase 5 CSP migration): behaviour unchanged. */
const API  = '';
const slug = window.location.pathname.split('/').filter(Boolean).pop() || '';

const CAT_ICONS = {
  'Food':'🍽️','Restaurant':'🍽️','Bakery':'🍞','Flowers':'🌸','Beauty':'💄',
  'Fashion':'👗','Electronics':'📱','Health':'💊','Retail':'🛒','Barber':'💈',
  'default':'🏪',
};
function icon(cat) {
  for (const [k,v] of Object.entries(CAT_ICONS))
    if ((cat||'').toLowerCase().includes(k.toLowerCase())) return v;
  return CAT_ICONS.default;
}
// Escapes quotes too: esc() is used inside double-quoted attributes (alt="...", src="..."),
// where a product name containing " would otherwise inject attributes (stored XSS).
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function sym(b){return b.currency_sym||b.currency_symbol||'$';}

async function load() {
  if (!slug || slug === 'store') {
    document.getElementById('loading').style.display='none';
    document.getElementById('error').style.display='block';
    document.getElementById('error').textContent='No store specified.';
    return;
  }
  try {
    const res  = await fetch(`${API}/api/store/${encodeURIComponent(slug)}`);
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    renderStore(data.business, data.products||[]);
  } catch(e) {
    document.getElementById('loading').style.display='none';
    document.getElementById('error').style.display='block';
    document.getElementById('error').textContent=
      `Store "${slug}" not found. It may have moved or been removed.`;
  }
}

function renderStore(biz, products) {
  document.title = `${biz.name} — WaziBot`;

  document.getElementById('store-logo').textContent    = icon(biz.category);
  document.getElementById('store-name').textContent    = biz.name||'';
  document.getElementById('store-category').textContent= biz.category||'';
  document.getElementById('store-tagline').textContent = biz.tagline||'';

  if (biz.logo_url && /^https?:\/\//i.test(biz.logo_url)) {
    const img = document.createElement('img');
    img.src   = biz.logo_url;
    img.style = 'width:56px;height:56px;border-radius:12px;object-fit:cover;';
    img.onerror = () => {};
    document.getElementById('store-logo').replaceWith(img);
  }

  const phone = biz.contact_phone||'';
  const waMsg = encodeURIComponent(`Hi! I'd like to order from ${biz.name}`);
  if (phone) {
    document.getElementById('order-btn').href =
      `https://wa.me/${phone.replace(/\D/g,'')}?text=${waMsg}`;
  } else {
    document.getElementById('order-btn').style.display='none';
  }
  document.getElementById('store-hero').style.display='block';
  document.getElementById('loading').style.display='none';

  document.getElementById('prod-count').textContent =
    `${products.length} product${products.length!==1?'s':''}`;

  if (!products.length) {
    document.getElementById('products-grid').innerHTML =
      '<div class="empty">No products listed yet.</div>';
  } else {
    const curr = esc(sym(biz));
    document.getElementById('products-grid').innerHTML = products.map(p => {
      const img = (p.image_url && /^https?:\/\//i.test(p.image_url))
        ? `<img class="prod-img" src="${esc(p.image_url)}" alt="${esc(p.name)}" loading="lazy"/>`
        : `<div class="prod-img-placeholder">📦</div>`;
      const avail = p.available===false
        ? '<div class="prod-unavail">Out of stock</div>' : '';
      return `
        <div class="prod-card">
          ${img}
          <div class="prod-body">
            <div class="prod-name">${esc(p.name)}</div>
            ${p.description?`<div class="prod-desc">${esc(p.description)}</div>`:''}
            <div class="prod-price">${curr}${parseFloat(p.price||0).toFixed(2)}</div>
            ${avail}
          </div>
        </div>`;
    }).join('');
  }
  document.getElementById('products-section').style.display='block';
}

load();

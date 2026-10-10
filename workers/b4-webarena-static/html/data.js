// Static product catalogue. Generated deterministically so traces are
// reproducible across runs / instance types. Roughly 60 items, each
// with name, price, rating, category, description.
window.SHOP_DATA = (function () {
  const CATS = ["audio", "computing", "wearables", "imaging", "accessory"];
  const ADJ  = ["pro", "ultra", "lite", "max", "core", "edge", "studio", "go"];
  const NOUN = ["phone", "laptop", "headphones", "camera", "watch",
                "tablet", "speaker", "monitor", "mouse", "keyboard"];
  const items = [];
  for (let i = 1; i <= 60; i++) {
    const adj  = ADJ[i % ADJ.length];
    const noun = NOUN[i % NOUN.length];
    const cat  = CATS[i % CATS.length];
    const price = +(9.99 + (i * 7.31) % 480).toFixed(2);
    const rating = +((i * 13) % 50 / 10).toFixed(1);
    items.push({
      id: i,
      sku: "SKU-" + i,
      name: `${noun} ${adj} ${i}`,
      price,
      rating,
      category: cat,
      desc: `Premium ${cat} product with adaptive performance and refined ` +
            `industrial design. Series ${adj}, build ${i}.`,
    });
  }
  return items;
})();

// Pre-canned reviews used on /product.html to give V8 a non-trivial
// JSON blob to walk. Kept inline so a real network round-trip isn't
// required (the static target is nginx with no app server).
window.SHOP_REVIEWS = [
  { author: "alice", rating: 5, text: "Exceeded expectations on every axis." },
  { author: "bob",   rating: 4, text: "Battery life is solid; UX could be tighter." },
  { author: "carol", rating: 5, text: "Best in class for the money." },
  { author: "dave",  rating: 3, text: "Works but build quality feels light." },
  { author: "eve",   rating: 4, text: "Setup was painless; recommended." },
  { author: "frank", rating: 2, text: "Returned after a week, software bugs." },
  { author: "grace", rating: 5, text: "Couldn't be happier - second purchase." },
  { author: "harry", rating: 4, text: "Display is fantastic in daylight." },
];

// Synthetic order history rendered on /orders.html. 25 rows, fixed.
window.SHOP_ORDERS = (function () {
  const out = [];
  for (let i = 1; i <= 25; i++) {
    out.push({
      id: 1000 + i,
      date: `2026-${String(((i % 12) + 1)).padStart(2, "0")}-` +
            `${String(((i * 3) % 27) + 1).padStart(2, "0")}`,
      items: ((i * 7) % 5) + 1,
      total: +(20 + (i * 11.7) % 240).toFixed(2),
      status: i % 6 === 0 ? "refunded" : "paid",
    });
  }
  return out;
})();

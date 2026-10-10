// Bench Shop client - intentionally non-trivial so the V8 engine has
// real JIT work during navigation: filtering, sorting, paginating,
// localStorage cart state, simple form validation. Roughly 5KB.
//
// Entry point: Shop.boot({ container, mode, pageSize }).

(function (global) {
  "use strict";

  const STORAGE_KEY = "bench-shop-cart-v1";

  function getCart() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) { return {}; }
  }
  function setCart(c) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(c)); } catch (e) {}
  }
  function cartCount(c) {
    return Object.values(c).reduce((s, v) => s + v, 0);
  }
  function cartTotal(c) {
    const items = global.SHOP_DATA;
    let t = 0;
    for (const id in c) {
      const it = items.find(x => String(x.id) === String(id));
      if (it) t += it.price * c[id];
    }
    return t;
  }

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === "class") n.className = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(c => n.appendChild(typeof c === "string"
      ? document.createTextNode(c) : c));
    return n;
  }

  function renderCard(item) {
    const card = el("div", { class: "product", "data-id": item.id });
    card.appendChild(el("div", { class: "product-img" }));
    card.appendChild(el("h3", null, [item.name]));
    card.appendChild(el("p",  { class: "product-price" }, ["$" + item.price.toFixed(2)]));
    card.appendChild(el("p",  { class: "product-rating" }, ["★ " + item.rating.toFixed(1)]));
    const link = el("a", {
      class: "product-link",
      href: "/product.html?id=" + item.id,
      "data-id": item.id,
    }, ["Details"]);
    card.appendChild(link);
    const btn = el("button", {
      class: "add-to-cart",
      type: "button",
      "data-id": item.id,
      "data-sku": item.sku,
    }, ["Add to cart"]);
    card.appendChild(btn);
    return card;
  }

  function getQueryParam(name) {
    const m = new RegExp("[?&]" + name + "=([^&#]*)").exec(location.search);
    return m ? decodeURIComponent(m[1].replace(/\+/g, " ")) : "";
  }

  function applySort(items, kind) {
    const copy = items.slice();
    if (kind === "price")  copy.sort((a, b) => a.price  - b.price);
    if (kind === "rating") copy.sort((a, b) => b.rating - a.rating);
    if (kind === "name")   copy.sort((a, b) => a.name.localeCompare(b.name));
    return copy;
  }

  function applyFilter(items, q) {
    if (!q) return items;
    const ql = q.toLowerCase();
    return items.filter(it =>
      it.name.toLowerCase().includes(ql)
      || it.category.toLowerCase().includes(ql)
      || it.desc.toLowerCase().includes(ql));
  }

  // ------------------- modes -------------------

  function mountIndexLike(opts, baseItems) {
    const container = $(opts.container);
    if (!container) return;
    const pageSize = opts.pageSize || 12;
    const state = { sort: null, page: 1 };

    function render() {
      const sorted = state.sort ? applySort(baseItems, state.sort) : baseItems;
      const total  = sorted.length;
      const shown  = sorted.slice(0, state.page * pageSize);
      container.innerHTML = "";
      shown.forEach(it => container.appendChild(renderCard(it)));
      const info = $("page-info");
      if (info) info.textContent = `${shown.length} / ${total}`;
    }

    document.querySelectorAll(".sort-price").forEach(b =>
      b.addEventListener("click", () => { state.sort = "price";  state.page = 1; render(); }));
    document.querySelectorAll(".sort-rating").forEach(b =>
      b.addEventListener("click", () => { state.sort = "rating"; state.page = 1; render(); }));
    document.querySelectorAll(".sort-name").forEach(b =>
      b.addEventListener("click", () => { state.sort = "name";   state.page = 1; render(); }));

    const more = $("load-more");
    if (more) more.addEventListener("click", () => { state.page += 1; render(); });

    container.addEventListener("click", e => {
      const t = e.target;
      if (t && t.matches(".add-to-cart")) {
        const id = t.getAttribute("data-id");
        const c = getCart();
        c[id] = (c[id] || 0) + 1;
        setCart(c);
        t.textContent = "Added";
      }
    });

    const sBtn = $("search-btn");
    if (sBtn) sBtn.addEventListener("click", () => {
      const q = ($("search-box") || {}).value || "";
      location.href = "/search.html?q=" + encodeURIComponent(q);
    });

    render();
  }

  function mountSearch(opts) {
    const q = getQueryParam("q");
    const titleEl = $("search-title");
    if (titleEl) titleEl.textContent = q ? `Results for "${q}"` : "Search results";
    const sb = $("search-box");
    if (sb) sb.value = q;
    mountIndexLike(opts, applyFilter(global.SHOP_DATA, q));
  }

  function mountProduct(opts) {
    const id = parseInt(getQueryParam("id"), 10) || 1;
    const item = global.SHOP_DATA.find(x => x.id === id) || global.SHOP_DATA[0];
    const detail = $("product-detail");
    if (detail) {
      detail.innerHTML = "";
      detail.appendChild(el("h1", null, [item.name]));
      detail.appendChild(el("p", { class: "product-price" }, ["$" + item.price.toFixed(2)]));
      detail.appendChild(el("p", { class: "product-rating" }, ["★ " + item.rating.toFixed(1)]));
      detail.appendChild(el("p", null, [item.desc]));
      detail.appendChild(el("button", {
        class: "add-to-cart btn primary",
        type: "button",
        "data-id": item.id,
      }, ["Add to cart"]));
    }
    const reviews = $("reviews");
    if (reviews) {
      global.SHOP_REVIEWS.forEach(r => {
        reviews.appendChild(el("li", null, [
          el("strong", null, [r.author + " "]),
          el("span", null, ["★ " + r.rating + " - "]),
          r.text,
        ]));
      });
    }
    document.addEventListener("click", e => {
      if (e.target && e.target.matches(".add-to-cart")) {
        const c = getCart();
        const aid = e.target.getAttribute("data-id") || String(item.id);
        c[aid] = (c[aid] || 0) + 1;
        setCart(c);
        e.target.textContent = "Added";
      }
    });
    // Recommend: random 6 items unrelated to current.
    const recs = global.SHOP_DATA.filter(x => x.id !== item.id).slice(0, 6);
    mountIndexLike(opts, recs);
  }

  function mountCart(opts) {
    const body = $(opts.container);
    if (!body) return;
    function render() {
      const c = getCart();
      body.innerHTML = "";
      let total = 0;
      Object.keys(c).forEach(id => {
        const it = global.SHOP_DATA.find(x => String(x.id) === String(id));
        if (!it) return;
        const sub = it.price * c[id];
        total += sub;
        body.appendChild(el("tr", { "data-id": id }, [
          el("td", null, [it.name]),
          el("td", null, [
            el("button", { class: "qty-dec", type: "button", "data-id": id }, ["-"]),
            el("span", { class: "qty" }, [String(c[id])]),
            el("button", { class: "qty-inc", type: "button", "data-id": id }, ["+"]),
          ]),
          el("td", null, ["$" + it.price.toFixed(2)]),
          el("td", null, ["$" + sub.toFixed(2)]),
        ]));
      });
      const totEl = $("cart-total");
      if (totEl) totEl.textContent = "$" + total.toFixed(2);
    }
    body.addEventListener("click", e => {
      const t = e.target;
      if (!t) return;
      const id = t.getAttribute("data-id");
      if (!id) return;
      const c = getCart();
      if (t.matches(".qty-inc")) c[id] = (c[id] || 0) + 1;
      else if (t.matches(".qty-dec")) c[id] = Math.max(0, (c[id] || 0) - 1);
      else return;
      if (c[id] === 0) delete c[id];
      setCart(c);
      render();
    });
    render();
  }

  function mountCheckout() {
    const btn = $("place-order");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const required = ["name", "email", "address"];
      for (const fid of required) {
        const v = ($(fid) || {}).value || "";
        if (!v.trim()) {
          $("order-status").textContent = "Please fill in " + fid;
          return;
        }
      }
      setCart({});
      $("order-status").textContent = "Order placed. Redirecting...";
      setTimeout(() => { location.href = "/orders.html"; }, 50);
    });
  }

  function mountOrders(opts) {
    const body = $(opts.container);
    if (!body) return;
    let filter = "all";

    function render() {
      body.innerHTML = "";
      const rows = global.SHOP_ORDERS.filter(o => filter === "all" || o.status === filter);
      rows.forEach(o => {
        body.appendChild(el("tr", { "data-id": o.id }, [
          el("td", null, ["#" + o.id]),
          el("td", null, [o.date]),
          el("td", null, [String(o.items)]),
          el("td", null, ["$" + o.total.toFixed(2)]),
          el("td", { class: "status-" + o.status }, [o.status]),
        ]));
      });
    }

    document.querySelectorAll(".tab-orders").forEach(t => {
      t.addEventListener("click", () => {
        document.querySelectorAll(".tab-orders").forEach(b => b.classList.remove("active"));
        t.classList.add("active");
        filter = t.getAttribute("data-tab") || "all";
        render();
      });
    });
    render();
  }

  function mountProfile(opts) {
    const btn = $("save-profile");
    if (btn) {
      btn.addEventListener("click", () => {
        const u = ($("username") || {}).value || "";
        const e = ($("email") || {}).value || "";
        $("profile-status").textContent = `Saved profile: ${u} <${e}>`;
      });
    }
    // Recently-viewed: deterministic top 8.
    mountIndexLike(opts, global.SHOP_DATA.slice(0, opts.pageSize || 8));
  }

  // ------------------- public boot dispatcher -------------------

  function boot(opts) {
    opts = opts || {};
    switch (opts.mode) {
      case "index":     mountIndexLike(opts, global.SHOP_DATA); break;
      case "search":    mountSearch(opts); break;
      case "product":   mountProduct(opts); break;
      case "cart":      mountCart(opts); break;
      case "checkout":  mountCheckout(); break;
      case "orders":    mountOrders(opts); break;
      case "profile":   mountProfile(opts); break;
      default: break;
    }
  }

  global.Shop = { boot: boot, getCart: getCart };

})(window);

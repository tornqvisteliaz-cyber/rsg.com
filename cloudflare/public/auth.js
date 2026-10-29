// Shared layout: header toolbar with dropdowns, footer and login state.
// <body data-layout="off"> skips the layout, data-footer="off" skips only the footer.
const LOGO = "/logo.svg";
const MENU = [
    ["Products", "/addons/", [["/addons/", "All products", "Everything we make"], ["/compatibility", "Compatibility", "Supported aircraft and hardware"], ["/roadmap", "Roadmap", "What we are working on"]]],
    ["Support", "/support", [["/support", "Help and FAQ", "Answers to common questions"], ["/installation", "Installation guide", "Set up HeavyPro step by step"], ["/downloads", "Downloads", "Installers and updates"], ["/contact", "Contact us", "Email and contact form"]]],
    ["News", "/newsletter", null],
    ["Company", "/about", [["/about", "About us", "Who we are"], ["/work-at-rsg", "Careers", "Work with us"], ["/contact", "Contact", "Get in touch"]]],
];
// Path prefix when the site is served from a sub-folder (GitHub Pages preview sets <meta name="site-base">).
const BASE = ((document.querySelector('meta[name="site-base"]') || {}).content || "").replace(/\/$/, "");
function withBase(path) { return BASE && path.startsWith("/") && !path.startsWith(BASE + "/") ? BASE + path : path; }
document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!BASE || !a) return;
    const href = a.getAttribute("href");
    if (href.startsWith("/") && !href.startsWith("//") && !href.startsWith(BASE + "/")) { e.preventDefault(); location.href = withBase(href); }
});

function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderLayout() {
    const body = document.body;
    const here = (BASE && location.pathname.startsWith(BASE) ? location.pathname.slice(BASE.length) || "/" : location.pathname).replace(/\.html$/, "").replace(/\/index$/, "/");
    const isHere = href => { const k = href.replace(/\/$/, ""); return k ? (here === k || here === k + "/" || here.startsWith(k + "/")) : here === "/"; };
    const menu = MENU.map(([label, href, items]) => {
        const active = isHere(href) || (items || []).some(i => isHere(i[0]));
        if (!items) return `<div class="menu-item"><a class="menu-link${active ? " active" : ""}" href="${href}">${label}</a></div>`;
        const links = items.map(([h, t, d]) => `<a href="${h}"><strong>${t}</strong><span>${d}</span></a>`).join("");
        return `<div class="menu-item has-drop"><a class="menu-link${active ? " active" : ""}" href="${href}">${label}<i></i></a><div class="drop">${links}</div></div>`;
    }).join("");
    const header = document.createElement("header");
    header.className = "header";
    header.innerHTML = `<div class="wrap"><a class="brand" href="/"><img src="${withBase(LOGO)}" alt="">Rotaidem</a>` +
        `<nav class="menu">${menu}</nav><div class="header-cta" id="auth-links"></div>` +
        `<button class="nav-toggle" aria-label="Menu" onclick="this.closest('.header').classList.toggle('open')"><span></span><span></span><span></span></button></div>`;
    body.prepend(header);

    if (body.dataset.footer !== "off") {
        const footer = document.createElement("footer");
        footer.className = "footer";
        footer.innerHTML = `<div class="wrap"><div class="footer-grid">
            <div><a class="brand" href="/"><img src="${withBase(LOGO)}" alt="">Rotaidem</a><p id="ft-text" style="margin:16px 0 22px;max-width:320px">An independent studio making software for Microsoft Flight Simulator 2024.</p>
                <form class="subscribe" data-subscribe><input type="email" name="email" placeholder="Email for release news" required aria-label="Email address"><button class="btn btn-sm">Subscribe</button><p class="msg"></p></form></div>
            <div><h4>Products</h4><a href="/heavypro">HeavyPro</a><a href="/addons/">All products</a><a href="/compatibility">Compatibility</a><a href="/roadmap">Roadmap</a></div>
            <div><h4>Support</h4><a href="/support">Help and FAQ</a><a href="/installation">Installation guide</a><a href="/downloads">Downloads</a><a href="/account">My account</a></div>
            <div><h4>Company</h4><a href="/about">About</a><a href="/newsletter">News</a><a href="/work-at-rsg">Careers</a><a href="/contact">Contact</a><a id="ft-mail" href="mailto:Eliaz@rotaidem.com">Eliaz@rotaidem.com</a></div>
        </div><div class="footer-bottom"><span>© ${new Date().getFullYear()} Rotaidem<span id="ft-socials" class="footer-socials"></span></span><span><a href="/terms">Terms of service</a><a href="/privacy">Privacy policy</a></span></div></div>`;
        body.append(footer);
    }
}

function paintAuth() {
    const box = document.getElementById("auth-links");
    if (!box) return;
    if (!localStorage.getItem("rsg_token")) {
        box.innerHTML = '<a class="text-link" href="/login">Log in</a><a class="btn btn-sm" href="/signup">Create account</a>';
        return;
    }
    const admin = localStorage.getItem("rsg_admin") === "true";
    box.innerHTML = (admin ? '<a class="text-link" href="/admin">Admin</a>' : "") +
        '<a class="text-link" href="#" onclick="rsgLogout();return false">Log out</a>' +
        '<a class="btn btn-sm" href="/account">' + esc(localStorage.getItem("rsg_name") || "My account") + "</a>";
}

// Download buttons go through the server: it counts the download and, for "licence holders only"
// products, checks the account first. Without the server the plain link (if any) still works.
async function rsgDownload(productId, fallback) {
    const token = localStorage.getItem("rsg_token");
    try {
        const res = await fetch(withBase("/api/download/" + encodeURIComponent(productId)), { method: "POST", headers: token ? { "X-Rotaidem-Token": token } : {} });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.hub) { location.href = withBase("/get-rotahub?product=" + encodeURIComponent(data.product || "")); return; } // downloads go through RotaHub
        if (res.ok && data.url) { location.href = data.url; return; }
        if (data.login) { if (confirm(data.error + "\n\nLog in now?")) location.href = withBase("/login?next=" + encodeURIComponent(location.pathname)); return; }
        if (data.error) { alert(data.error); return; }
    } catch (err) { /* fall back to the plain link */ }
    if (fallback) location.href = fallback;
}
document.addEventListener("click", e => {
    const b = e.target.closest("[data-dl]");
    if (!b) return;
    e.preventDefault();
    rsgDownload(b.dataset.dl, b.getAttribute("href") && b.getAttribute("href") !== "#" ? b.getAttribute("href") : "");
});

// Buy with Stripe Checkout (products with a price at checkout): logged in -> Stripe's payment page.
async function rsgBuy(productId, button) {
    const token = localStorage.getItem("rsg_token");
    if (!token) { location.href = withBase("/login?next=" + encodeURIComponent(location.pathname + location.search)); return; }
    if (button) { button.dataset.label = button.dataset.label || button.textContent; button.textContent = "Opening checkout…"; button.style.pointerEvents = "none"; }
    try {
        const res = await fetch(withBase("/api/checkout/" + encodeURIComponent(productId)), { method: "POST", headers: { "X-Rotaidem-Token": token } });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.url) { location.href = data.url; return; }
        if (data.login) { location.href = withBase("/login?next=" + encodeURIComponent(location.pathname + location.search)); return; }
        alert(data.error || "The checkout could not be opened. Please try again.");
        if (data.owned) location.href = withBase("/account");
    } catch (err) { alert("The checkout could not be opened. Check your connection and try again."); }
    if (button) { button.textContent = button.dataset.label; button.style.pointerEvents = ""; }
}

function rsgLogout() {
    const token = localStorage.getItem("rsg_token");
    if (token) fetch("/api/logout", { method: "POST", headers: { "X-Rotaidem-Token": token } }).catch(() => {});
    localStorage.removeItem("rsg_token");
    localStorage.removeItem("rsg_name");
    localStorage.removeItem("rsg_admin");
    location.href = withBase("/");
}

function saveSession(data) {
    localStorage.setItem("rsg_token", data.token);
    localStorage.setItem("rsg_name", data.name || "");
    localStorage.setItem("rsg_admin", data.is_admin ? "true" : "false");
}

let FALLBACK_IMAGE = "/img/approach.jpg";
// Full https links, uploaded images (/media/12.jpg) and the built-in artwork (/img/...).
function safeImage(url) { return /^(https:\/\/.+\..+|\/media\/\d+\.(jpg|png|webp|gif)|\/img\/[\w.-]+)$/.test(url || "") ? withBase(url) : withBase(FALLBACK_IMAGE); }
const STATUS_LABEL = { published: "Available", coming_soon: "Coming soon", in_development: "In development" };

function productCard(p) {
    return `<a class="product" href="/addons/view?id=${encodeURIComponent(p.id)}"><div class="img"><img src="${esc(safeImage(p.image_url))}" alt="" loading="lazy"></div>` +
        `<div class="meta"><h3>${esc(p.name)}</h3><span class="price">${esc(p.price || "")}</span></div>` +
        `<div class="sub">${esc(p.tagline || p.simulator || "")}</div><span class="badge status ${esc(p.status)}">${esc(STATUS_LABEL[p.status] || p.status || "")}</span></a>`;
}

// Any <form data-subscribe> with an email field signs the visitor up for product emails.
document.addEventListener("submit", async e => {
    const form = e.target.closest("[data-subscribe]");
    if (!form) return;
    e.preventDefault();
    const msg = form.querySelector(".msg");
    const res = await fetch("/api/subscribe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ email: form.email.value, source: location.pathname }) }).catch(() => null);
    const data = res ? await res.json().catch(() => ({})) : {};
    msg.className = "msg " + (res && res.ok ? "ok" : "error");
    msg.textContent = res && res.ok ? "Thanks, you are on the list." : (data.error || "Could not subscribe right now. Please try again later.");
    if (res && res.ok) form.reset();
});

// Page texts edited in the admin: plain text, new lines become line breaks, **word** becomes bold.
// Built with DOM nodes (never innerHTML), so saved text cannot inject markup.
const ORIGINAL_TEXT = new WeakMap();
function renderText(el, value) {
    if (!ORIGINAL_TEXT.has(el)) ORIGINAL_TEXT.set(el, el.innerHTML);
    el.textContent = "";
    String(value).split("\n").forEach((line, i) => {
        if (i) el.append(document.createElement("br"));
        line.split(/\*\*(.+?)\*\*/).forEach((part, j) => {
            if (!part) return;
            if (j % 2) { const b = document.createElement("strong"); b.textContent = part; el.append(b); }
            else el.append(document.createTextNode(part));
        });
    });
}
function applyTexts(texts) {
    document.querySelectorAll("[data-text]").forEach(el => {
        const value = texts[el.dataset.text];
        if (value) renderText(el, value);
        else if (ORIGINAL_TEXT.has(el)) { el.innerHTML = ORIGINAL_TEXT.get(el); ORIGINAL_TEXT.delete(el); }
    });
}

// General settings from the admin: contact email, footer text and social links in the footer.
const SOCIAL_NAMES = { discord: "Discord", youtube: "YouTube", instagram: "Instagram", x: "X", facebook: "Facebook", twitch: "Twitch", tiktok: "TikTok" };
function applyConfig(config) {
    const text = document.getElementById("ft-text"), mail = document.getElementById("ft-mail"), socials = document.getElementById("ft-socials");
    if (text && config.footer_text) text.textContent = config.footer_text;
    if (mail && config.contact_email) { mail.textContent = config.contact_email; mail.href = "mailto:" + config.contact_email; }
    if (socials) socials.innerHTML = Object.entries(config.socials || {}).filter(([k, url]) => SOCIAL_NAMES[k] && /^https:\/\//.test(url))
        .map(([k, url]) => `<a href="${esc(url)}" target="_blank" rel="noopener">${SOCIAL_NAMES[k]}</a>`).join("");
    applyMaintenance(config.maintenance || {});
}
// Maintenance mode: visitors see a back-soon page; logged-in staff and the log in pages are left alone.
function applyMaintenance(m) {
    const old = document.getElementById("maintenance");
    const exempt = localStorage.getItem("rsg_admin") === "true" || /^\/(login|admin|forgot-password|reset-password)/.test(location.pathname);
    if (!m.enabled || exempt) { if (old) old.remove(); return; }
    const box = old || document.createElement("div");
    box.id = "maintenance"; box.className = "maintenance";
    box.innerHTML = `<div><img src="${withBase(LOGO)}" alt="" style="height:28px;margin:0 auto 28px"><h1>Back soon</h1><p class="lead" style="margin:0 auto 28px">${esc(m.message || "We are updating the website. Please check back in a few minutes.")}</p><a class="text-link" href="/login">Staff log in</a></div>`;
    if (!old) document.body.append(box);
}

// Look & media from the admin: accent colour, logo, HeavyPro screenshots and video, launch countdown,
// and home page sections switched off.
const ACCENT_COLOURS = { amber: ["#f2a900", "#ffbd2e", "242, 169, 0"], sky: ["#5ab0ff", "#8cc8ff", "90, 176, 255"], mint: ["#4cc38a", "#7ad7aa", "76, 195, 138"],
    rose: ["#ff6b81", "#ff94a5", "255, 107, 129"], violet: ["#a78bfa", "#c4b5fd", "167, 139, 250"] };
function applyExtras(x) {
    const root = document.documentElement, accent = ACCENT_COLOURS[x.accent];
    ["--accent", "--accent-hover", "--accent-rgb"].forEach((name, i) => accent ? root.style.setProperty(name, accent[i]) : root.style.removeProperty(name));
    if (x.logo) document.querySelectorAll(".brand img").forEach(img => { img.src = safeImage(x.logo); });
    root.dataset.hide = (x.hide || []).join(" ");
    const media = document.querySelector("[data-media]");
    if (media) {
        const shots = x.gallery || [];
        media.querySelector("[data-gallery]").innerHTML = shots.map(u => `<button type="button" class="shot" data-full="${esc(safeImage(u))}" aria-label="Show larger"><img src="${esc(safeImage(u))}" alt="" loading="lazy"></button>`).join("");
        media.querySelector("[data-video]").innerHTML = x.video ? `<button type="button" class="video-play" data-yt="${esc(x.video)}"><span class="play" aria-hidden="true"></span>Play video<small>Plays from YouTube</small></button>` : "";
        media.hidden = !shots.length && !x.video;
    }
    startCountdown(x.launch || {});
}
let countdownTimer = null;
function startCountdown(launch) {
    const el = document.querySelector("[data-countdown]");
    if (!el) return;
    clearInterval(countdownTimer);
    const end = Date.parse(launch.at || ""), pad = n => String(n).padStart(2, "0");
    const tick = () => {
        const left = end - Date.now();
        if (!launch.enabled || !(left > 0)) { el.hidden = true; clearInterval(countdownTimer); return; }
        const d = Math.floor(left / 864e5), h = Math.floor(left / 36e5) % 24, m = Math.floor(left / 6e4) % 60, sec = Math.floor(left / 1e3) % 60;
        el.innerHTML = `<span>HeavyPro launches in</span><b>${d}<small>days</small></b><b>${pad(h)}<small>hours</small></b><b>${pad(m)}<small>min</small></b><b>${pad(sec)}<small>sec</small></b>`;
        el.hidden = false;
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
}
document.addEventListener("click", e => {
    const shot = e.target.closest(".shot"), play = e.target.closest(".video-play"), box = e.target.closest(".lightbox");
    if (shot) {
        const lb = document.createElement("div");
        lb.className = "lightbox"; lb.innerHTML = `<img src="${esc(shot.dataset.full)}" alt="">`;
        document.body.append(lb);
    } else if (box) box.remove();
    else if (play) play.outerHTML = `<div class="video-frame"><iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(play.dataset.yt)}?autoplay=1&rel=0" title="Video" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>`;
});
document.addEventListener("keydown", e => { if (e.key === "Escape") document.querySelectorAll(".lightbox").forEach(b => b.remove()); });

// Roadmap and FAQ edited in the admin replace the built-in lists on those pages.
function applyLists(site) {
    const roadmap = document.querySelector("[data-roadmap]");
    if (roadmap && (site.roadmap || []).length) roadmap.innerHTML = site.roadmap.map(i =>
        `<div><span class="when">${esc(i.stage)}</span><div><h3>${esc(i.title)}</h3>${i.text ? `<p class="muted" style="white-space:pre-line">${esc(i.text)}</p>` : ""}</div></div>`).join("");
    // Admin -> Aircraft: the supported aircraft on the home, Compatibility and HeavyPro pages
    const aircraft = site.aircraft || [];
    if (aircraft.length) {
        const home = document.querySelector('[data-aircraft="home"]'), table = document.querySelector('[data-aircraft="table"]'), names = document.querySelector('[data-aircraft="names"]');
        if (home) home.innerHTML = aircraft.filter(a => a.home).map(a => `<div><strong>${esc(a.name)}</strong><span>${esc(a.home_text || a.profile)}</span></div>`).join("");
        if (table) table.innerHTML = aircraft.map(a => `<tr><td>${esc(a.name)}</td><td>${esc(a.profile)}</td><td class="muted">${esc(a.note)}</td></tr>`).join("");
        if (names) names.textContent = aircraft.map(a => a.name).join(", ");
    }
    const faq = document.querySelector("[data-faq]");
    if (faq && (site.faq || []).length) faq.innerHTML = site.faq.map(i =>
        `<details><summary>${esc(i.q)}</summary><p style="white-space:pre-line">${esc(i.a)}</p></details>`).join("");
}

// Site settings from the admin panel: images per slot, page texts and the announcement banner.
function applyContent(site) {
    applyTexts(site.texts || {});
    applyLists(site);
    if (site.config) applyConfig(site.config);
    if (site.extras) applyExtras(site.extras);
    const images = site.images || {};
    if (images.product) FALLBACK_IMAGE = images.product;
    document.querySelectorAll("[data-image]").forEach(el => {
        const url = images[el.dataset.image];
        if (!url) return;
        if (el.tagName === "IMG") el.src = safeImage(url);
        else el.style.backgroundImage = `url('${safeImage(url).replace(/'/g, "%27")}')`;
    });
}
function applyBanner(site) {
    const banner = site.announcement || {};
    let dismissed = "";
    try { dismissed = sessionStorage.getItem("rsg_banner_closed") || ""; } catch (err) {}
    const d = new Date(), today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const inWindow = (!banner.from || today >= banner.from) && (!banner.until || today <= banner.until);
    if (banner.enabled && banner.text && inWindow && dismissed !== banner.text && document.body.dataset.layout !== "off") {
        const bar = document.createElement("div");
        bar.className = "announce";
        const text = banner.link ? `<a href="${esc(banner.link)}">${esc(banner.text)}</a>` : esc(banner.text);
        bar.innerHTML = `<div class="wrap"><span>${text}</span><button type="button" aria-label="Close">×</button></div>`;
        bar.querySelector("button").onclick = () => { bar.remove(); try { sessionStorage.setItem("rsg_banner_closed", banner.text); } catch (err) {} };
        document.body.prepend(bar);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    if (document.body.dataset.layout !== "off") renderLayout();
    paintAuth();
    // The footer only exists from here on, so its settings go in now.
    if (window.RSG_SITE) {
        if (window.RSG_SITE.config) applyConfig(window.RSG_SITE.config);
        if (window.RSG_SITE.extras) applyExtras(window.RSG_SITE.extras); // the header logo exists from here on
        applyBanner(window.RSG_SITE); return;
    }
    // Hosts without /site-settings.js: ask the API instead (the page shows the defaults until then).
    fetch(withBase("/api/site")).then(r => r.ok ? r.json() : null).then(site => {
        if (site) { applyContent(site); applyBanner(site); }
    }).catch(() => {});
});

// /site-settings.js (loaded in <head>) already holds the pictures and texts: put them in now,
// before the page is first drawn, so visitors never see the default wording or pictures.
if (window.RSG_SITE) applyContent(window.RSG_SITE);

// Shared layout: header toolbar with dropdowns, footer and login state.
// <body data-layout="off"> skips the layout, data-footer="off" skips only the footer.
const LOGO = "/logo.svg";
const MENU = [
    ["Products", "/addons/", [["/heavypro", "HeavyPro", "Airliner control feel for MSFS 2024"], ["/addons/", "All products", "Everything we make"], ["/compatibility", "Compatibility", "Supported aircraft and hardware"], ["/roadmap", "Roadmap", "What we are working on"]]],
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
            <div><a class="brand" href="/"><img src="${withBase(LOGO)}" alt="">Rotaidem</a><p style="margin:16px 0 22px;max-width:320px">An independent studio making software for Microsoft Flight Simulator 2024.</p>
                <form class="subscribe" data-subscribe><input type="email" name="email" placeholder="Email for release news" required aria-label="Email address"><button class="btn btn-sm">Subscribe</button><p class="msg"></p></form></div>
            <div><h4>Products</h4><a href="/heavypro">HeavyPro</a><a href="/addons/">All products</a><a href="/compatibility">Compatibility</a><a href="/roadmap">Roadmap</a></div>
            <div><h4>Support</h4><a href="/support">Help and FAQ</a><a href="/installation">Installation guide</a><a href="/downloads">Downloads</a><a href="/account">My account</a></div>
            <div><h4>Company</h4><a href="/about">About</a><a href="/newsletter">News</a><a href="/work-at-rsg">Careers</a><a href="/contact">Contact</a><a href="mailto:Eliaz@rotaidem.com">Eliaz@rotaidem.com</a></div>
        </div><div class="footer-bottom"><span>© ${new Date().getFullYear()} Rotaidem</span><span><a href="/terms">Terms of service</a><a href="/privacy">Privacy policy</a></span></div></div>`;
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
        `<div class="sub">${esc(p.tagline || p.simulator || "")} · ${esc(STATUS_LABEL[p.status] || p.status || "")}</div></a>`;
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

// Site settings from the admin panel: images per slot and the announcement banner.
function applySiteSettings(site) {
    const images = site.images || {};
    if (images.product) FALLBACK_IMAGE = images.product;
    document.querySelectorAll("[data-image]").forEach(el => {
        const url = images[el.dataset.image];
        if (!url) return;
        if (el.tagName === "IMG") el.src = safeImage(url);
        else el.style.backgroundImage = `url('${safeImage(url).replace(/'/g, "%27")}')`;
    });
    const banner = site.announcement || {};
    let dismissed = "";
    try { dismissed = sessionStorage.getItem("rsg_banner_closed") || ""; } catch (err) {}
    if (banner.enabled && banner.text && dismissed !== banner.text && document.body.dataset.layout !== "off") {
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
    fetch(withBase("/api/site")).then(r => r.ok ? r.json() : null).then(site => { if (site) applySiteSettings(site); }).catch(() => {});
});

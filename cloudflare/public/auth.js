// Shared layout: header toolbar with dropdowns, footer and login state.
// <body data-layout="off"> skips the layout, data-footer="off" skips only the footer.
const LOGO = "https://i.postimg.cc/rwKST91B/rsgsoftware.png";
const MENU = [
    ["Products", "/addons/", [["/heavypro", "HeavyPro", "Airliner control feel for MSFS 2024"], ["/addons/", "All products", "Everything we make"], ["/compatibility", "Compatibility", "Supported aircraft and hardware"], ["/roadmap", "Roadmap", "What we are working on"]]],
    ["Support", "/support", [["/support", "Help and FAQ", "Answers to common questions"], ["/installation", "Installation guide", "Set up HeavyPro step by step"], ["/downloads", "Downloads", "Installers and updates"], ["/contact", "Contact us", "Email and contact form"]]],
    ["News", "/newsletter", null],
    ["Company", "/about", [["/about", "About us", "Who we are"], ["/work-at-rsg", "Careers", "Work with us"], ["/contact", "Contact", "Get in touch"]]],
];
function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderLayout() {
    const body = document.body;
    const here = location.pathname.replace(/\.html$/, "").replace(/\/index$/, "/");
    const isHere = href => { const k = href.replace(/\/$/, ""); return k ? (here === k || here === k + "/" || here.startsWith(k + "/")) : here === "/"; };
    const menu = MENU.map(([label, href, items]) => {
        const active = isHere(href) || (items || []).some(i => isHere(i[0]));
        if (!items) return `<div class="menu-item"><a class="menu-link${active ? " active" : ""}" href="${href}">${label}</a></div>`;
        const links = items.map(([h, t, d]) => `<a href="${h}"><strong>${t}</strong><span>${d}</span></a>`).join("");
        return `<div class="menu-item has-drop"><a class="menu-link${active ? " active" : ""}" href="${href}">${label}<i></i></a><div class="drop">${links}</div></div>`;
    }).join("");
    const header = document.createElement("header");
    header.className = "header";
    header.innerHTML = `<div class="wrap"><a class="brand" href="/"><img src="${LOGO}" alt="">Rotaidem</a>` +
        `<nav class="menu">${menu}</nav><div class="header-cta" id="auth-links"></div>` +
        `<button class="nav-toggle" aria-label="Menu" onclick="this.closest('.header').classList.toggle('open')"><span></span><span></span><span></span></button></div>`;
    body.prepend(header);

    if (body.dataset.footer !== "off") {
        const footer = document.createElement("footer");
        footer.className = "footer";
        footer.innerHTML = `<div class="wrap"><div class="footer-grid">
            <div><a class="brand" href="/"><img src="${LOGO}" alt="">Rotaidem</a><p style="margin-top:16px;max-width:300px">An independent studio making software for Microsoft Flight Simulator 2024.</p></div>
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
    if (token) fetch("/api/logout", { method: "POST", headers: { Authorization: "Bearer " + token } }).catch(() => {});
    localStorage.removeItem("rsg_token");
    localStorage.removeItem("rsg_name");
    localStorage.removeItem("rsg_admin");
    location.href = "/";
}

function saveSession(data) {
    localStorage.setItem("rsg_token", data.token);
    localStorage.setItem("rsg_name", data.name || "");
    localStorage.setItem("rsg_admin", data.is_admin ? "true" : "false");
}

const FALLBACK_IMAGE = "https://i.postimg.cc/28WybSM2/metroliner.jpg";
function safeImage(url) { return /^https?:\/\/.+\..+/.test(url || "") ? url : FALLBACK_IMAGE; }
const STATUS_LABEL = { published: "Available", coming_soon: "Coming soon", in_development: "In development" };

function productCard(p) {
    return `<a class="product" href="/addons/view?id=${encodeURIComponent(p.id)}"><div class="img"><img src="${esc(safeImage(p.image_url))}" alt="" loading="lazy"></div>` +
        `<div class="meta"><h3>${esc(p.name)}</h3><span class="price">${esc(p.price || "")}</span></div>` +
        `<div class="sub">${esc(p.tagline || p.simulator || "")} · ${esc(STATUS_LABEL[p.status] || p.status || "")}</div></a>`;
}

document.addEventListener("DOMContentLoaded", () => {
    if (document.body.dataset.layout !== "off") renderLayout();
    paintAuth();
});

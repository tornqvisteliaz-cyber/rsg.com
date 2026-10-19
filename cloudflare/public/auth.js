// Shared layout: top bar, header, footer and login state.
// <body data-layout="off"> skips the layout, data-footer="off" skips only the footer.
const LOGO = "https://i.postimg.cc/rwKST91B/rsgsoftware.png";
const MENU = [["/heavypro", "HeavyPro"], ["/addons/", "Products"], ["/newsletter", "News"], ["/support", "Support"], ["/about", "About"]];

function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderLayout() {
    const body = document.body;
    const here = location.pathname.replace(/\.html$/, "").replace(/\/index$/, "/");
    const menu = MENU.map(([href, label]) => {
        const key = href.replace(/\/$/, "");
        const active = here === href || here.endsWith(key) || here.includes(key + "/");
        return `<a href="${href}"${active ? ' class="active"' : ""}>${label}</a>`;
    }).join("");

    const top = document.createElement("div");
    top.className = "topbar";
    top.innerHTML = `<div class="wrap"><span class="left">Software for Microsoft Flight Simulator 2024</span><span id="auth-links"></span></div>`;
    const header = document.createElement("header");
    header.className = "header";
    header.innerHTML = `<div class="wrap"><a class="brand" href="/"><img src="${LOGO}" alt="">Rotaidem</a>` +
        `<nav class="menu">${menu}</nav><div class="header-cta"><a class="btn btn-sm" href="/addons/">Browse products</a></div>` +
        `<button class="nav-toggle" onclick="this.closest('.header').classList.toggle('open')">Menu</button></div>`;
    body.prepend(header);
    body.prepend(top);

    if (body.dataset.footer !== "off") {
        const footer = document.createElement("footer");
        footer.className = "footer";
        footer.innerHTML = `<div class="wrap"><div class="footer-grid">
            <div><a class="brand" href="/"><img src="${LOGO}" alt="">Rotaidem</a><p style="margin-top:16px;max-width:300px">An independent studio making software for Microsoft Flight Simulator 2024.</p></div>
            <div><h4>Products</h4><a href="/heavypro">HeavyPro</a><a href="/addons/">All products</a><a href="/newsletter">News</a></div>
            <div><h4>Help</h4><a href="/support">Support and FAQ</a><a href="/contact">Contact</a><a href="/account">My account</a></div>
            <div><h4>Company</h4><a href="/about">About</a><a href="/work-at-rsg">Careers</a><a href="mailto:Eliaz@rotaidem.com">Eliaz@rotaidem.com</a></div>
        </div><div class="footer-bottom"><span>© ${new Date().getFullYear()} Rotaidem</span><span><a href="/terms">Terms of service</a><a href="/privacy">Privacy policy</a></span></div></div>`;
        body.append(footer);
    }
}

function paintAuth() {
    const box = document.getElementById("auth-links");
    if (!box) return;
    if (!localStorage.getItem("rsg_token")) {
        box.innerHTML = '<a href="/support">Help</a><a href="/login">Log in</a><a href="/signup">Create account</a>';
        return;
    }
    const admin = localStorage.getItem("rsg_admin") === "true";
    box.innerHTML = (admin ? '<a href="/admin">Admin</a>' : "") +
        '<a href="/account">' + esc(localStorage.getItem("rsg_name") || "My account") + "</a>" +
        '<a href="#" onclick="rsgLogout();return false">Log out</a>';
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

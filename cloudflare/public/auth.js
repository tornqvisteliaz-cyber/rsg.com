// Shared layout: nav, footer, auth buttons, scroll effects.
// Pages opt out of the footer with <body data-footer="off"> and get a solid nav with data-nav="solid".
const LOGO = "https://i.postimg.cc/rwKST91B/rsgsoftware.png";
const NAV_LINKS = [["/", "Home"], ["/addons/", "Products"], ["/about", "About"], ["/newsletter", "News"], ["/work-at-rsg", "Careers"], ["/contact", "Contact"]];

function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderLayout() {
    const body = document.body;
    const here = location.pathname.replace(/\.html$/, "").replace(/\/index$/, "/");
    const links = NAV_LINKS.map(([href, label]) => {
        const active = href === "/" ? here === "/" : here.startsWith(href.replace(/\/$/, ""));
        return `<a href="${href}"${active ? ' class="active"' : ""}>${label}</a>`;
    }).join("");
    const nav = document.createElement("nav");
    nav.className = "nav" + (body.dataset.nav === "solid" ? " solid" : "");
    nav.innerHTML = `<div class="wrap"><a class="brand" href="/"><img src="${LOGO}" alt="">Rotaidem</a>` +
        `<button class="nav-toggle" aria-label="Menu" onclick="this.closest('.nav').classList.toggle('open')">☰</button>` +
        `<div class="nav-links">${links}</div><div class="nav-auth" id="auth-buttons"></div></div>`;
    body.prepend(nav);
    window.addEventListener("scroll", () => nav.classList.toggle("scrolled", window.scrollY > 40), { passive: true });

    if (body.dataset.footer !== "off") {
        const footer = document.createElement("footer");
        footer.className = "footer";
        footer.innerHTML = `<div class="wrap"><div class="footer-grid">
            <div><a class="brand" href="/"><img src="${LOGO}" alt="">Rotaidem</a><p class="muted" style="margin-top:14px;max-width:320px">Home of HeavyPro: study-level heavy aircraft for Microsoft Flight Simulator 2024.</p></div>
            <div><h4>Products</h4><a href="/addons/">All products</a><a href="/#heavypro">HeavyPro</a><a href="/newsletter">Development news</a></div>
            <div><h4>Company</h4><a href="/about">About</a><a href="/work-at-rsg">Careers</a><a href="/contact">Contact</a></div>
            <div><h4>Account</h4><a href="/account">My account</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a></div>
        </div><div class="footer-bottom"><span>© ${new Date().getFullYear()} Rotaidem. All rights reserved.</span><span>Built for Microsoft Flight Simulator 2024</span></div></div>`;
        body.append(footer);
    }
}

function paintAuth() {
    const box = document.getElementById("auth-buttons");
    if (!box) return;
    const token = localStorage.getItem("rsg_token");
    const admin = localStorage.getItem("rsg_admin") === "true";
    if (!token) {
        box.innerHTML = '<a href="/login" class="btn btn-ghost btn-sm">Log in</a><a href="/signup" class="btn btn-sm">Sign up</a>';
        return;
    }
    box.innerHTML = (admin ? '<a href="/admin" class="btn btn-ghost btn-sm">Admin</a>' : "") +
        '<a href="/account" class="btn btn-sm">' + esc(localStorage.getItem("rsg_name") || "Account") + "</a>" +
        '<button class="btn btn-ghost btn-sm" onclick="rsgLogout()">Log out</button>';
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

function revealOnScroll() {
    const items = document.querySelectorAll(".reveal");
    if (!("IntersectionObserver" in window)) { items.forEach(el => el.classList.add("in")); return; }
    const io = new IntersectionObserver(entries => entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: .12 });
    items.forEach(el => io.observe(el));
}

const FALLBACK_IMAGE = "https://i.postimg.cc/28WybSM2/metroliner.jpg";
function safeImage(url) { return /^https?:\/\/.+\..+/.test(url || "") ? url : FALLBACK_IMAGE; }
const STATUS_LABEL = { published: "Available", coming_soon: "Coming soon", in_development: "In development" };

document.addEventListener("DOMContentLoaded", () => {
    if (document.body.dataset.layout !== "off") renderLayout();
    paintAuth();
    revealOnScroll();
});

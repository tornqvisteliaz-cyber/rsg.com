// Link previews (Admin -> Site settings -> Link previews), same as server.py's with_share_tags: the
// picture, title and text Discord, Messenger and others show for a link go into the page's <head>,
// because those apps never run the page's scripts. public/_routes.json sends the pages here.
const PAGES = { "/": ["hero"], "/heavypro": ["heavypro"], "/addons/": ["product"], "/addons/view": ["product"], "/downloads": ["header"], "/get-rotahub": ["header"],
  "/compatibility": ["header"], "/installation": ["header"], "/support": ["header"], "/roadmap": ["header"], "/newsletter": ["header"], "/about": ["about"],
  "/contact": ["header"], "/work-at-rsg": ["header"] };
const IMAGE_SLOTS = { hero: "/img/hero.jpg", feature: "/img/approach.jpg", heavypro: "/img/clouds.jpg", header: "/img/night.jpg", login: "/img/clouds.jpg", about: "/img/approach.jpg", product: "/img/approach.jpg" };
const esc = v => String(v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function pageOf(pathname) {
  let p = pathname.replace(/\.html$/, "").replace(/\/index$/, "/");
  if (p === "/addons") p = "/addons/";
  return p in PAGES ? p : "";
}
async function setting(env, key) {
  const row = await env.DB.prepare("SELECT value FROM meta WHERE key = ?").bind(key).first().catch(() => null);
  try { return row ? JSON.parse(row.value) || {} : {}; } catch (err) { return {}; }
}

export async function onRequest({ request, next, env }) {
  const res = await next();
  const url = new URL(request.url), page = pageOf(url.pathname);
  if (request.method !== "GET" || !page || !env.DB || !(res.headers.get("content-type") || "").includes("text/html")) return res;
  const share = await setting(env, "site.share"), images = await setting(env, "site.images");
  const own = (share.pages || {})[page] || {}, slot = PAGES[page][0];
  const image = own.image || (share.default || {}).image || images[slot] || IMAGE_SLOTS[slot];
  const base = String(env.SITE_URL || url.origin).replace(/\/$/, ""), absolute = u => u.startsWith("/") ? base + u : u;
  let title = "", description = "";
  return new HTMLRewriter()
    .on("title", { text(t) { title += t.text; } })
    .on('meta[name="description"]', { element(e) { description = e.getAttribute("content") || ""; } })
    .on("head", { element(e) { e.onEndTag(end => {
      const plain = s => s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").trim();
      const tags = [["og:type", "website"], ["og:site_name", "Rotaidem"], ["og:title", own.title || plain(title) || "Rotaidem"], ["og:description", own.description || plain(description)],
        ["og:url", absolute(page)], ["og:image", absolute(image)]].filter(t => t[1]);
      end.before("<!-- share -->" + tags.map(([k, v]) => `<meta property="${k}" content="${esc(v)}">`).join("") +
        `<meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="${esc(absolute(image))}"><!-- /share -->`, { html: true });
    }); } })
    .transform(res);
}

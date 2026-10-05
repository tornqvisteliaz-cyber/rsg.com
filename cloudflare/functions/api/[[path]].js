export async function onRequest(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  try {
    await ensure(env);
    if (request.method === "POST" && path === "/api/login") return login(request, env);
    if (request.method === "POST" && path === "/api/signup") return signup(request, env);
    if (request.method === "GET" && path === "/api/products") return products(request, env);
    if (request.method === "GET" && path === "/api/liveries") return liveries(request, env);
    if (request.method === "POST" && path === "/api/save-product") return saveProduct(request, env);
    if (request.method === "POST" && path === "/api/save-livery") return saveLivery(request, env);
    if (request.method === "GET" && path === "/api/newsletter") return newsletter(env);
    if (request.method === "POST" && path === "/api/newsletter") return saveNewsletter(request, env);
    if (request.method === "POST" && path === "/api/contact") return contact(request, env);
    return json({ error: "Not found" }, 404);
  } catch (err) {
    return json({ error: String(err && err.message || err) }, 500);
  }
}

async function ensure(env) {
  if (!env.DB) return;
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'Customer', token TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, simulator TEXT, version TEXT, folder_name TEXT, download_url TEXT, image_url TEXT, price TEXT, description TEXT, buy_url TEXT, status TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS liveries (id TEXT PRIMARY KEY, name TEXT, aircraft TEXT, folder_name TEXT, download_url TEXT, image_url TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, content TEXT, image_url TEXT, date TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, message TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM products").first();
  if (!count || count.n === 0) {
    await env.DB.prepare("INSERT INTO products (id, name, simulator, version, folder_name, image_url, price, description, buy_url, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").bind(
      "seabee", "Republic RC-3 Seabee", "MSFS 2024", "0.1.0-dev", "rsg-seabee",
      "https://i.postimg.cc/28WybSM2/metroliner.jpg", "$29.99",
      "Amphibious flying boat for Microsoft Flight Simulator 2024.",
      "/aircraft/seabee", "in_development"
    ).run();
  }
}

async function login(request, env) {
  const body = await request.json();
  const email = String(body.email || "").trim().toLowerCase();
  const password = String(body.password || "");
  let user = await env.DB.prepare("SELECT * FROM users WHERE email = ?").bind(email).first();
  if (!user && env.ADMIN_EMAIL && email === String(env.ADMIN_EMAIL).toLowerCase() && password === env.ADMIN_PASSWORD) {
    const hash = await hashPassword(password);
    const token = crypto.randomUUID();
    await env.DB.prepare("INSERT INTO users (name, email, password_hash, role, token) VALUES (?, ?, ?, 'Owner', ?)").bind(env.ADMIN_USERNAME || "eliaz", email, hash, token).run();
    return json({ token, name: env.ADMIN_USERNAME || "eliaz", role: "Owner", is_admin: true });
  }
  if (!user || !(await verifyPassword(password, user.password_hash))) return json({ error: "Invalid email or password" }, 401);
  const token = crypto.randomUUID();
  await env.DB.prepare("UPDATE users SET token = ? WHERE id = ?").bind(token, user.id).run();
  const isAdmin = ["Owner", "Admin", "Support"].includes(user.role);
  return json({ token, name: user.name, email: user.email, role: user.role, is_admin: isAdmin });
}

async function signup(request, env) {
  const body = await request.json();
  const name = String(body.name || "").trim();
  const email = String(body.email || "").trim().toLowerCase();
  const password = String(body.password || "");
  if (!name || !email || password.length < 6) return json({ error: "Name, email and a 6 character password are required" }, 400);
  const token = crypto.randomUUID();
  try {
    await env.DB.prepare("INSERT INTO users (name, email, password_hash, role, token) VALUES (?, ?, ?, 'Customer', ?)").bind(name, email, await hashPassword(password), token).run();
  } catch {
    return json({ error: "Email already exists" }, 400);
  }
  return json({ token, name, role: "Customer", is_admin: false });
}

async function products(request, env) {
  const user = await currentUser(request, env);
  if (!user) return json({ error: "Unauthorized" }, 401);
  const rows = await env.DB.prepare("SELECT * FROM products ORDER BY name").all();
  const owned = user.role !== "Customer" || env.RSG_DEV_UNLOCK === "true";
  return json({ products: (rows.results || []).map((item) => ({ ...item, owned, download_url: owned ? item.download_url : "" })) });
}

async function liveries(request, env) {
  const user = await currentUser(request, env);
  if (!user) return json({ error: "Unauthorized" }, 401);
  const rows = await env.DB.prepare("SELECT * FROM liveries ORDER BY name").all();
  const owned = user.role !== "Customer" || env.RSG_DEV_UNLOCK === "true";
  return json({ liveries: (rows.results || []).map((item) => ({ ...item, owned, download_url: owned ? item.download_url : "" })) });
}

async function saveProduct(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const body = await request.json();
  const name = String(body.name || "").trim();
  if (!name) return json({ error: "Name is required" }, 400);
  const id = slug(body.id || name);
  await env.DB.prepare("INSERT INTO products (id, name, simulator, version, folder_name, download_url, image_url, price, description, buy_url, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, version=excluded.version, folder_name=excluded.folder_name, download_url=excluded.download_url, image_url=excluded.image_url, price=excluded.price, description=excluded.description, buy_url=excluded.buy_url").bind(
    id, name, body.simulator || "MSFS 2024", body.version || "0.1.0", body.folder_name || `rsg-${id}`, body.download_url || "", body.image_url || "", body.price || "$29.99", body.description || "", body.buy_url || "/aircraft/seabee", body.status || "in_development"
  ).run();
  return json({ ok: true });
}

async function saveLivery(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const body = await request.json();
  const name = String(body.name || "").trim();
  if (!name) return json({ error: "Name is required" }, 400);
  const id = slug(body.id || name);
  await env.DB.prepare("INSERT INTO liveries (id, name, aircraft, folder_name, download_url, image_url) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, aircraft=excluded.aircraft, folder_name=excluded.folder_name, download_url=excluded.download_url, image_url=excluded.image_url").bind(
    id, name, body.aircraft || "Republic RC-3 Seabee", body.folder_name || `rsg-${id}`, body.download_url || "", body.image_url || ""
  ).run();
  return json({ ok: true });
}

async function newsletter(env) {
  const rows = await env.DB.prepare("SELECT * FROM posts ORDER BY id DESC").all();
  return json({ posts: rows.results || [] });
}

async function saveNewsletter(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const body = await request.json();
  if (!body.title || !body.content) return json({ error: "Title and content are required" }, 400);
  await env.DB.prepare("INSERT INTO posts (title, content, image_url, date) VALUES (?, ?, ?, ?)").bind(body.title, body.content, body.image_url || "", new Date().toISOString()).run();
  return json({ ok: true });
}

async function contact(request, env) {
  const body = await request.json();
  await env.DB.prepare("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)").bind(body.name || "", body.email || "", body.message || "").run();
  return json({ ok: true });
}

async function currentUser(request, env) {
  const token = (request.headers.get("Authorization") || "").replace("Bearer ", "").trim();
  if (!token) return null;
  return env.DB.prepare("SELECT * FROM users WHERE token = ?").bind(token).first();
}

function slug(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || crypto.randomUUID().slice(0, 8);
}

async function hashPassword(password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", salt, iterations: 100000, hash: "SHA-256" }, key, 256);
  return `${btoa(String.fromCharCode(...salt))}.${btoa(String.fromCharCode(...new Uint8Array(bits)))}`;
}

async function verifyPassword(password, stored) {
  if (!stored || !stored.includes(".")) return false;
  const [saltB64, hashB64] = stored.split(".");
  const salt = Uint8Array.from(atob(saltB64), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", salt, iterations: 100000, hash: "SHA-256" }, key, 256);
  const next = btoa(String.fromCharCode(...new Uint8Array(bits)));
  return next === hashB64;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

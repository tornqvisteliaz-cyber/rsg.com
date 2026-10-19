export async function onRequest(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/$/, "");
  const method = request.method;
  if (method === "OPTIONS") return json({ ok: true });

  try {
    await ensure(env);
    if (method === "POST" && path === "/api/login") return login(request, env);
    if (method === "POST" && path === "/api/signup") return signup(request, env);
    if (method === "POST" && path === "/api/logout") return logout(request, env);
    if (method === "GET" && path === "/api/me") return me(request, env);
    if (method === "POST" && path === "/api/account/settings") return updateSettings(request, env);
    if (method === "GET" && path === "/api/account/orders") return customerOrders(request, env);
    if (method === "GET" && path === "/api/products") return products(request, env);
    if (method === "GET" && path === "/api/liveries") return liveries(request, env);
    if (method === "POST" && path === "/api/save-product") return saveProduct(request, env);
    if (method === "POST" && path === "/api/save-livery") return saveLivery(request, env);
    if (method === "DELETE" && path.startsWith("/api/products/")) return deleteProduct(request, env, decodeURIComponent(path.split("/").pop()));
    if (method === "GET" && path === "/api/newsletter") return newsletter(env);
    if (method === "POST" && path === "/api/newsletter") return saveNewsletter(request, env);
    if (method === "DELETE" && path.startsWith("/api/newsletter/")) return deleteNewsletter(request, env, path.split("/").pop());
    if (method === "POST" && path === "/api/contact") return contact(request, env);
    if (method === "GET" && path === "/api/admin/overview") return adminOverview(request, env);
    if (method === "GET" && path === "/api/admin/customers") return adminCustomers(request, env);
    if (method === "GET" && path === "/api/admin/orders") return adminOrders(request, env);
    if (method === "POST" && path === "/api/admin/orders") return adminSaveOrder(request, env);
    if (method === "GET" && path === "/api/admin/messages") return adminMessages(request, env);
    if (method === "DELETE" && path.startsWith("/api/admin/messages/")) return adminDeleteMessage(request, env, path.split("/").pop());
    if (method === "GET" && path === "/api/admin/audit") return adminAudit(request, env);
    if (method === "POST" && /^\/api\/admin\/users\/\d+\/role$/.test(path)) return adminSetRole(request, env, Number(path.split("/")[4]));
    if (method === "POST" && path === "/api/licenses/import") return licensesImport(request, env);
    if (method === "POST" && path === "/api/licenses/activate") return licenseActivate(request, env);
    if (method === "POST" && path === "/api/licenses/validate") return licenseValidate(request, env);
    if (method === "POST" && path === "/api/licenses/deactivate") return licenseDeactivate(request, env);
    if (method === "GET" && path === "/api/account/licenses") return accountLicenses(request, env);
    if (method === "POST" && path === "/api/admin/licenses/generate") return adminGenerateLicenses(request, env);
    if (method === "GET" && path === "/api/admin/licenses") return adminLicenses(request, env);
    if (method === "GET" && /^\/api\/admin\/licenses\/\d+$/.test(path)) return adminLicenseDetail(request, env, Number(path.split("/")[4]));
    if (method === "POST" && /^\/api\/admin\/licenses\/\d+$/.test(path)) return adminUpdateLicense(request, env, Number(path.split("/")[4]));
    if (method === "DELETE" && /^\/api\/admin\/licenses\/\d+\/activations\/\d+$/.test(path)) return adminResetActivation(request, env, Number(path.split("/")[4]), Number(path.split("/")[6]));
    if (method === "POST" && path === "/api/subscribe") return subscribe(request, env);
    if (method === "POST" && path === "/api/unsubscribe") return unsubscribe(request, env);
    if (method === "POST" && path === "/api/admin/audience") return adminAudience(request, env);
    if (method === "POST" && path === "/api/admin/broadcast") return adminBroadcast(request, env);
    if (method === "GET" && path === "/api/admin/campaigns") return adminCampaigns(request, env);
    if (method === "GET" && path === "/api/admin/subscribers") return adminSubscribers(request, env);
    if (method === "DELETE" && path.startsWith("/api/admin/subscribers/")) return adminDeleteSubscriber(request, env, Number(path.split("/").pop()));
    return json({ error: "Not found" }, 404);
  } catch (err) {
    return json({ error: String(err && err.message || err) }, 500);
  }
}

async function ensure(env) {
  if (!env.DB) return;
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'Customer', token TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT, simulator TEXT, version TEXT, folder_name TEXT, download_url TEXT, image_url TEXT, price TEXT, description TEXT, buy_url TEXT, status TEXT, images TEXT)").run();
  try { await env.DB.prepare("ALTER TABLE products ADD COLUMN images TEXT").run(); } catch (err) {}
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS liveries (id TEXT PRIMARY KEY, name TEXT, aircraft TEXT, folder_name TEXT, download_url TEXT, image_url TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, content TEXT, image_url TEXT, date TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, message TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, status TEXT DEFAULT 'Completed', payment_status TEXT DEFAULT 'Paid', amount NUMERIC DEFAULT 29.99, invoice_file TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT, target TEXT, ip_address TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();

  for (const col of ["tagline TEXT", "features TEXT", "sort_order INTEGER DEFAULT 0"]) {
    try { await env.DB.prepare("ALTER TABLE products ADD COLUMN " + col).run(); } catch (err) {}
  }
  try { await env.DB.prepare("ALTER TABLE orders ADD COLUMN product_id TEXT").run(); } catch (err) {}
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS licenses (id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE, product_id TEXT, email TEXT, customer_id INTEGER, order_id INTEGER, status TEXT DEFAULT 'active', max_activations INTEGER DEFAULT 2, note TEXT, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS license_activations (id INTEGER PRIMARY KEY AUTOINCREMENT, license_id INTEGER, machine_id TEXT, machine_name TEXT, first_seen TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE (license_id, machine_id))").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, source TEXT, unsubscribed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, product_id TEXT, audience TEXT, sent_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0, admin_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  // One-time wipe of the old catalog for the HeavyPro relaunch.
  const reset = await env.DB.prepare("SELECT value FROM meta WHERE key = 'catalog_reset_heavypro'").first();
  if (!reset) {
    await env.DB.prepare("DELETE FROM products").run();
    await env.DB.prepare("INSERT INTO meta (key, value) VALUES ('catalog_reset_heavypro', ?)").bind(new Date().toISOString()).run();
  }
}

async function login(request, env) {
  const body = await request.json();
  const input = String(body.email || "").trim().toLowerCase();
  const password = String(body.password || "");
  let user = await env.DB.prepare("SELECT * FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?").bind(input, input).first();
  if (!user && env.ADMIN_PASSWORD && password === env.ADMIN_PASSWORD) {
    const adminEmail = (env.ADMIN_EMAIL || "admin@rsgsoftware.com").toLowerCase();
    const adminUser = (env.ADMIN_USERNAME || "eliaz").toLowerCase();
    if (input === adminEmail || input === adminUser) {
      const hash = await hashPassword(password);
      const token = crypto.randomUUID();
      await env.DB.prepare("INSERT INTO users (name, email, password_hash, role, token) VALUES (?, ?, ?, 'Owner', ?)").bind(
        env.ADMIN_USERNAME || "eliaz", env.ADMIN_EMAIL || "admin@rsgsoftware.com", hash, token
      ).run();
      return json({ token, name: env.ADMIN_USERNAME || "eliaz", email: env.ADMIN_EMAIL || "admin@rsgsoftware.com", role: "Owner", is_admin: true });
    }
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
  } catch { return json({ error: "Email already exists" }, 400); }
  return json({ token, name, email, role: "Customer", is_admin: false });
}

async function logout(request, env) {
  const token = bearerToken(request);
  if (token) await env.DB.prepare("UPDATE users SET token = NULL WHERE token = ?").bind(token).run();
  return json({ ok: true });
}

async function me(request, env) {
  const user = await currentUser(request, env);
  if (!user) return json({ error: "Unauthorized" }, 401);
  const isAdmin = ["Owner", "Admin", "Support"].includes(user.role);
  const orderCount = await env.DB.prepare("SELECT COUNT(*) AS count FROM orders WHERE customer_id = ?").bind(user.id).first();
  return json({ user: { id: user.id, name: user.name, email: user.email, role: user.role, is_admin: isAdmin, orders_count: orderCount ? orderCount.count : 0 } });
}

async function updateSettings(request, env) {
  const user = await currentUser(request, env);
  if (!user) return json({ error: "Unauthorized" }, 401);
  const body = await request.json();
  const name = String(body.name || "").trim();
  const currentPassword = String(body.current_password || "");
  const newPassword = String(body.new_password || "");
  if (!name) return json({ error: "Name is required" }, 400);
  if (newPassword) {
    if (newPassword.length < 6) return json({ error: "New password must be at least 6 characters" }, 400);
    if (!currentPassword || !(await verifyPassword(currentPassword, user.password_hash))) return json({ error: "Current password is required and must be correct" }, 400);
    await env.DB.prepare("UPDATE users SET name = ?, password_hash = ? WHERE id = ?").bind(name, await hashPassword(newPassword), user.id).run();
  } else {
    await env.DB.prepare("UPDATE users SET name = ? WHERE id = ?").bind(name, user.id).run();
  }
  return json({ ok: true, name });
}

async function customerOrders(request, env) {
  const user = await currentUser(request, env);
  if (!user) return json({ error: "Unauthorized" }, 401);
  const rows = await env.DB.prepare("SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC").bind(user.id).all();
  return json({ orders: rows.results || [] });
}

async function products(request, env) {
  const rows = await env.DB.prepare("SELECT * FROM products ORDER BY sort_order, name").all();
  return json({ products: (rows.results || []).map(withImages) });
}

async function liveries(request, env) {
  const rows = await env.DB.prepare("SELECT * FROM liveries ORDER BY name").all();
  return json({ liveries: rows.results || [] });
}

async function saveProduct(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const body = await request.json();
  const name = String(body.name || "").trim();
  if (!name) return json({ error: "Name is required" }, 400);
  const id = slug(body.id || name);
  const images = JSON.stringify(listImages(body.images));
  await env.DB.prepare("INSERT INTO products (id, name, simulator, version, folder_name, download_url, image_url, images, price, description, buy_url, status, tagline, features, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, simulator=excluded.simulator, version=excluded.version, folder_name=excluded.folder_name, download_url=excluded.download_url, image_url=excluded.image_url, images=excluded.images, price=excluded.price, description=excluded.description, buy_url=excluded.buy_url, status=excluded.status, tagline=excluded.tagline, features=excluded.features, sort_order=excluded.sort_order").bind(
    id, name, body.simulator || "MSFS 2024", body.version || "0.1.0", body.folder_name || `rotaidem-${id}`, body.download_url || "", body.image_url || "", images, body.price || "", body.description || "", body.buy_url || "mailto:Eliaz@rotaidem.com", body.status || "in_development", body.tagline || "", JSON.stringify(listImages(body.features)), Number(body.sort_order) || 0
  ).run();
  await audit(env, user, "save_product", id);
  return json({ ok: true, id, page: "/addons/view?id=" + id });
}

async function deleteProduct(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  await env.DB.prepare("DELETE FROM products WHERE id = ?").bind(id).run();
  await audit(env, user, "delete_product", id);
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
    id, name, body.aircraft || "", body.folder_name || `rotaidem-${id}`, body.download_url || "", body.image_url || ""
  ).run();
  return json({ ok: true, id });
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
  if (body.id) {
    await env.DB.prepare("UPDATE posts SET title = ?, content = ?, image_url = ? WHERE id = ?").bind(body.title, body.content, body.image_url || "", Number(body.id)).run();
  } else {
    await env.DB.prepare("INSERT INTO posts (title, content, image_url, date) VALUES (?, ?, ?, ?)").bind(
      body.title, body.content, body.image_url || "", new Date().toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" })
    ).run();
  }
  await audit(env, user, "save_post", body.title);
  return json({ ok: true });
}

async function deleteNewsletter(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  await env.DB.prepare("DELETE FROM posts WHERE id = ?").bind(id).run();
  return json({ ok: true });
}

async function contact(request, env) {
  const body = await request.json();
  await env.DB.prepare("INSERT INTO messages (name, email, message) VALUES (?, ?, ?)").bind(body.name || "", body.email || "", body.message || "").run();
  return json({ ok: true });
}

async function adminOverview(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const prodCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM products").first();
  const custCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM users WHERE role = 'Customer'").first();
  const orderCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM orders").first();
  const postCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM posts").first();
  const msgCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM messages").first();
  const revenue = await env.DB.prepare("SELECT COALESCE(SUM(amount), 0) AS n FROM orders WHERE payment_status = 'Paid'").first();
  const subCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM subscribers WHERE unsubscribed = 0").first();
  const licCount = await env.DB.prepare("SELECT COUNT(*) AS n FROM licenses WHERE status = 'active'").first();
  return json({ name: user.name, role: user.role, products: prodCount?.n || 0, customers: custCount?.n || 0, orders: orderCount?.n || 0, posts: postCount?.n || 0, messages: msgCount?.n || 0, subscribers: subCount?.n || 0, licenses: licCount?.n || 0, revenue: Number(revenue?.n || 0), email_ready: !!resendKey(env) });
}

async function adminCustomers(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT id, name, email, role, created_at FROM users ORDER BY id DESC").all();
  return json({ customers: rows.results || [] });
}

async function adminOrders(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT orders.*, users.name as customer_name, users.email as customer_email FROM orders LEFT JOIN users ON orders.customer_id = users.id ORDER BY orders.id DESC").all();
  return json({ orders: rows.results || [] });
}

async function adminSaveOrder(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const body = await request.json();
  if (!Number(body.customer_id)) return json({ error: "Customer and amount must be numbers" }, 400);
  await env.DB.prepare("INSERT INTO orders (customer_id, product_id, status, payment_status, amount) VALUES (?, ?, ?, ?, ?)").bind(Number(body.customer_id), body.product_id || "", body.status || "Completed", body.payment_status || "Paid", Number(body.amount) || 0).run();
  await audit(env, user, "create_order", body.customer_id);
  if (body.create_license && body.product_id) {
    const customer = await env.DB.prepare("SELECT id, email FROM users WHERE id = ?").bind(Number(body.customer_id)).first();
    const order = await env.DB.prepare("SELECT MAX(id) AS id FROM orders WHERE customer_id = ?").bind(Number(body.customer_id)).first();
    let key = "";
    while (!key) {
      const candidate = generateKey(body.license_prefix || "HPRO");
      if (await insertLicense(env, candidate, body.product_id, customer ? customer.email : "", Number(body.customer_id), order ? order.id : null, 2, "Created with order", user.name)) key = candidate;
    }
    await audit(env, user, "create_licenses", key + " for order");
    return json({ ok: true, license_key: key });
  }
  return json({ ok: true });
}

async function adminMessages(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT * FROM messages ORDER BY id DESC").all();
  return json({ messages: rows.results || [] });
}

async function adminDeleteMessage(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  await env.DB.prepare("DELETE FROM messages WHERE id = ?").bind(Number(id)).run();
  return json({ ok: true });
}

async function adminAudit(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT audit_logs.*, users.name AS admin_name FROM audit_logs LEFT JOIN users ON audit_logs.admin_id = users.id ORDER BY audit_logs.id DESC LIMIT 100").all();
  return json({ logs: rows.results || [] });
}

async function adminSetRole(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role !== "Owner") return json({ error: "Owner only" }, 403);
  const body = await request.json();
  if (!["Customer", "Support", "Admin", "Owner"].includes(body.role)) return json({ error: "Unknown role" }, 400);
  if (id === user.id) return json({ error: "You cannot change your own role" }, 400);
  await env.DB.prepare("UPDATE users SET role = ?, token = NULL WHERE id = ?").bind(body.role, id).run();
  await audit(env, user, "set_role", id + ":" + body.role);
  return json({ ok: true });
}

async function audit(env, user, action, target) {
  await env.DB.prepare("INSERT INTO audit_logs (admin_id, action, target, ip_address) VALUES (?, ?, ?, ?)").bind(user.id, action, String(target || "").slice(0, 200), "").run();
}

// ---------- licences (same rules as server.py and licensing/src/Rotaidem.Licensing/LicenseKey.cs) ----------
const KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const KEY_RE = /^([A-Z0-9]{2,8})-([A-Z2-9]{4})-([A-Z2-9]{4})-([A-Z2-9]{4})$/;

function keyChecksum(prefix, body11) {
  let total = 0;
  for (const ch of prefix) total += ch.charCodeAt(0);
  for (let i = 0; i < body11.length; i++) total += (2 * i + 1) * KEY_ALPHABET.indexOf(body11[i]);
  return KEY_ALPHABET[total % 32];
}
function generateKey(prefix = "HPRO") {
  prefix = String(prefix).toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8) || "HPRO";
  const bytes = crypto.getRandomValues(new Uint8Array(11));
  const body = [...bytes].map(b => KEY_ALPHABET[b % 32]).join("");
  const full = body + keyChecksum(prefix, body);
  return `${prefix}-${full.slice(0, 4)}-${full.slice(4, 8)}-${full.slice(8, 12)}`;
}
function normalizeKey(value) {
  const key = String(value || "").replace(/\s+/g, "").toUpperCase();
  const m = KEY_RE.exec(key);
  if (!m) return "";
  const chars = m[2] + m[3] + m[4];
  if ([...chars].some(c => !KEY_ALPHABET.includes(c)) || keyChecksum(m[1], chars.slice(0, 11)) !== chars[11]) return "";
  return key;
}
const clampInt = (v, d, lo, hi) => { const n = parseInt(v, 10); return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : d; };

async function licenseToolUser(request, env) {
  const supplied = request.headers.get("X-License-Api-Key") || "";
  const expected = String(env.LICENSE_API_KEY || "").trim();
  if (expected.length >= 16 && supplied && supplied === expected) return { id: null, name: "License generator", role: "Tool" };
  const user = await currentUser(request, env);
  return user && ["Owner", "Admin"].includes(user.role) ? user : null;
}
async function insertLicense(env, key, productId, email, customerId, orderId, max, note, createdBy) {
  if (await env.DB.prepare("SELECT id FROM licenses WHERE license_key = ?").bind(key).first()) return false;
  await env.DB.prepare("INSERT INTO licenses (license_key, product_id, email, customer_id, order_id, max_activations, note, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    .bind(key, productId, String(email || "").trim().toLowerCase(), customerId, orderId, max, note, createdBy).run();
  return true;
}
async function customerIdFor(env, email) {
  if (!email) return null;
  const row = await env.DB.prepare("SELECT id FROM users WHERE LOWER(email) = ?").bind(email).first();
  return row ? row.id : null;
}

async function licensesImport(request, env) {
  const user = await licenseToolUser(request, env);
  if (!user) return json({ error: "Not allowed. Check the generator's API key." }, 403);
  const items = (await request.json()).licenses || [];
  if (!Array.isArray(items) || items.length > 1000) return json({ error: "Send a list of at most 1000 licences" }, 400);
  const created = [], skipped = [], invalid = [];
  for (const item of items) {
    const key = normalizeKey(item.key);
    if (!key) { invalid.push(String(item.key || "").slice(0, 40)); continue; }
    const email = String(item.email || "").trim().toLowerCase();
    const ok = await insertLicense(env, key, String(item.product_id || "heavypro").slice(0, 60), email, await customerIdFor(env, email), null, clampInt(item.max_activations, 2, 1, 100), String(item.note || "").slice(0, 200), user.name);
    (ok ? created : skipped).push(key);
  }
  if (created.length) await audit(env, user, "import_licenses", `${created.length} keys`);
  return json({ ok: true, created, skipped, invalid });
}

async function adminGenerateLicenses(request, env) {
  const user = await currentUser(request, env);
  if (!user || !["Owner", "Admin"].includes(user.role)) return json({ error: "Only the Owner or an Admin can create licences" }, 403);
  const body = await request.json();
  const count = clampInt(body.count, 1, 1, 500);
  const prefix = String(body.prefix || "HPRO").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8) || "HPRO";
  const email = String(body.email || "").trim().toLowerCase();
  if (email && !EMAIL_RE.test(email)) return json({ error: "Enter a valid email address or leave it empty" }, 400);
  const customerId = await customerIdFor(env, email);
  const keys = [];
  while (keys.length < count) {
    const key = generateKey(prefix);
    if (await insertLicense(env, key, String(body.product_id || "heavypro").slice(0, 60), email, customerId, null, clampInt(body.max_activations, 2, 1, 100), String(body.note || "").slice(0, 200), user.name)) keys.push(key);
  }
  await audit(env, user, "create_licenses", `${count} x ${prefix}` + (email ? ` for ${email}` : ""));
  return json({ ok: true, keys });
}

async function adminLicenses(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT licenses.*, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations FROM licenses ORDER BY licenses.id DESC LIMIT 2000").all();
  return json({ licenses: rows.results || [] });
}

async function adminLicenseDetail(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const lic = await env.DB.prepare("SELECT * FROM licenses WHERE id = ?").bind(id).first();
  if (!lic) return json({ error: "Licence not found" }, 404);
  lic.machines = (await env.DB.prepare("SELECT * FROM license_activations WHERE license_id = ? ORDER BY id").bind(id).all()).results || [];
  return json({ license: lic });
}

async function adminUpdateLicense(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || !["Owner", "Admin"].includes(user.role)) return json({ error: "Only the Owner or an Admin can change licences" }, 403);
  const lic = await env.DB.prepare("SELECT * FROM licenses WHERE id = ?").bind(id).first();
  if (!lic) return json({ error: "Licence not found" }, 404);
  const body = await request.json();
  const status = body.status || lic.status;
  if (!["active", "revoked"].includes(status)) return json({ error: "Status must be active or revoked" }, 400);
  const email = String(body.email ?? lic.email ?? "").trim().toLowerCase();
  await env.DB.prepare("UPDATE licenses SET status = ?, email = ?, customer_id = ?, max_activations = ?, note = ? WHERE id = ?")
    .bind(status, email, await customerIdFor(env, email), clampInt(body.max_activations, lic.max_activations, 1, 100), String(body.note ?? lic.note ?? "").slice(0, 200), id).run();
  await audit(env, user, "update_license", `${lic.license_key} (${status})`);
  return json({ ok: true });
}

async function adminResetActivation(request, env, licenseId, activationId) {
  const user = await currentUser(request, env);
  if (!user || !["Owner", "Admin"].includes(user.role)) return json({ error: "Only the Owner or an Admin can change licences" }, 403);
  await env.DB.prepare("DELETE FROM license_activations WHERE id = ? AND license_id = ?").bind(activationId, licenseId).run();
  await audit(env, user, "reset_activation", licenseId);
  return json({ ok: true });
}

async function licenseForRequest(request, env) {
  const body = await request.json().catch(() => ({}));
  const key = normalizeKey(body.key);
  if (!key) return { error: json({ valid: false, reason: "invalid_format", error: "That is not a valid licence key." }, 400) };
  const lic = await env.DB.prepare("SELECT * FROM licenses WHERE license_key = ?").bind(key).first();
  if (!lic) return { error: json({ valid: false, reason: "not_found", error: "This licence key does not exist." }, 404) };
  if (lic.status !== "active") return { error: json({ valid: false, reason: "revoked", error: "This licence key has been revoked." }, 403) };
  const product = String(body.product || "").trim();
  if (product && product !== lic.product_id) return { error: json({ valid: false, reason: "wrong_product", error: "This licence key is for another product." }, 403) };
  const machine = String(body.machine_id || "").trim().slice(0, 128);
  if (!machine) return { error: json({ valid: false, reason: "missing_machine", error: "machine_id is required." }, 400) };
  return { lic, machine, body };
}
const activationCount = async (env, id) => ((await env.DB.prepare("SELECT COUNT(*) AS n FROM license_activations WHERE license_id = ?").bind(id).first()) || {}).n || 0;
const licenseReply = (lic, used) => json({ valid: true, product_id: lic.product_id, activations_used: used, max_activations: lic.max_activations, checked_at: new Date().toISOString().replace(/\.\d+Z$/, "Z") });

async function licenseActivate(request, env) {
  const r = await licenseForRequest(request, env);
  if (r.error) return r.error;
  const name = String(r.body.machine_name || "").slice(0, 100);
  const existing = await env.DB.prepare("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?").bind(r.lic.id, r.machine).first();
  let used = await activationCount(env, r.lic.id);
  if (existing) {
    await env.DB.prepare("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP, machine_name = ? WHERE id = ?").bind(name, existing.id).run();
  } else if (used >= r.lic.max_activations) {
    return json({ valid: false, reason: "activation_limit", activations_used: used, max_activations: r.lic.max_activations, error: `This key is already active on ${used} computer(s). Deactivate one first or contact support.` }, 403);
  } else {
    await env.DB.prepare("INSERT INTO license_activations (license_id, machine_id, machine_name) VALUES (?, ?, ?)").bind(r.lic.id, r.machine, name).run();
    used += 1;
  }
  return licenseReply(r.lic, used);
}

async function licenseValidate(request, env) {
  const r = await licenseForRequest(request, env);
  if (r.error) return r.error;
  const existing = await env.DB.prepare("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?").bind(r.lic.id, r.machine).first();
  if (!existing) return json({ valid: false, reason: "not_activated", error: "This computer has not been activated with this key." }, 403);
  await env.DB.prepare("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP WHERE id = ?").bind(existing.id).run();
  return licenseReply(r.lic, await activationCount(env, r.lic.id));
}

async function licenseDeactivate(request, env) {
  const r = await licenseForRequest(request, env);
  if (r.error) return r.error;
  await env.DB.prepare("DELETE FROM license_activations WHERE license_id = ? AND machine_id = ?").bind(r.lic.id, r.machine).run();
  return json({ ok: true });
}

async function accountLicenses(request, env) {
  const user = await currentUser(request, env);
  if (!user) return json({ error: "Unauthorized" }, 401);
  const rows = await env.DB.prepare("SELECT licenses.id, license_key, product_id, status, max_activations, licenses.created_at, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY licenses.id DESC")
    .bind(user.id, String(user.email || "").toLowerCase()).all();
  return json({ licenses: rows.results || [] });
}

// ---------- email broadcasts ----------
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const AUDIENCES = ["accounts", "buyers", "subscribers", "contacts"];
const AUDIENCE_SQL = {
  accounts: "SELECT email FROM users",
  buyers: "SELECT DISTINCT users.email AS email FROM orders JOIN users ON users.id = orders.customer_id",
  subscribers: "SELECT email FROM subscribers WHERE unsubscribed = 0",
  contacts: "SELECT DISTINCT email FROM messages",
};
const mailFrom = env => String(env.MAIL_FROM || "").trim() || "Rotaidem <no-reply@rotaidem.com>";
const resendKey = env => String(env.RESEND_API_KEY || "").trim();

function explainResendError(env, status, raw) {
  let message = "";
  try { message = String(JSON.parse(raw).message || ""); } catch (err) {}
  const text = (message || raw).toLowerCase();
  const from = mailFrom(env);
  if (text.includes("own email address") || (text.includes("verify a domain") && from.includes("resend.dev")))
    return "Resend is still in test mode. With the onboarding@resend.dev sender it only delivers to the address you signed up to Resend with, so send the test to that address. To email anyone else, verify rotaidem.com at resend.com/domains and set MAIL_FROM, e.g. Rotaidem <no-reply@rotaidem.com>.";
  if (text.includes("not verified"))
    return `The sender domain in MAIL_FROM (${from}) is not verified in Resend yet. Finish the DNS setup at resend.com/domains, or remove MAIL_FROM to use Resend's test sender.`;
  if ((status === 401 || status === 403) && /api key|unauthorized|restricted/.test(text))
    return "Resend rejected the API key. Check RESEND_API_KEY (it starts with re_ and must have sending access), save, and redeploy.";
  if (text.includes("from") && /invalid|format/.test(text))
    return `MAIL_FROM is not a valid sender (${from}). Use the format: Rotaidem <no-reply@rotaidem.com>`;
  return `Resend said (${status}): ${message || raw.slice(0, 200)}`;
}
const escHtml = v => String(v == null ? "" : v).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function unsubscribeToken(env, email) {
  const secret = env.SECRET_KEY || env.ADMIN_PASSWORD || "rotaidem-dev-secret";
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(email.toLowerCase()));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

async function subscribe(request, env) {
  const body = await request.json();
  const email = String(body.email || "").trim().toLowerCase();
  if (!EMAIL_RE.test(email) || email.length > 200) return json({ error: "Please enter a valid email address" }, 400);
  await env.DB.prepare("INSERT INTO subscribers (email, source) VALUES (?, ?) ON CONFLICT(email) DO UPDATE SET unsubscribed = 0").bind(email, String(body.source || "").slice(0, 100)).run();
  return json({ ok: true });
}

async function unsubscribe(request, env) {
  const body = await request.json();
  const email = String(body.email || "").trim().toLowerCase();
  if (!email || (await unsubscribeToken(env, email)) !== String(body.token || "")) return json({ error: "This unsubscribe link is not valid" }, 400);
  await env.DB.prepare("INSERT INTO subscribers (email, source, unsubscribed) VALUES (?, 'unsubscribe', 1) ON CONFLICT(email) DO UPDATE SET unsubscribed = 1").bind(email).run();
  return json({ ok: true });
}

async function collectRecipients(env, groups, extra) {
  let found = [];
  for (const g of groups) if (AUDIENCE_SQL[g]) found = found.concat(((await env.DB.prepare(AUDIENCE_SQL[g]).all()).results || []).map(r => r.email));
  found = found.concat(String(extra || "").split(/[\s,;]+/));
  const blocked = new Set(((await env.DB.prepare("SELECT email FROM subscribers WHERE unsubscribed = 1").all()).results || []).map(r => r.email));
  const seen = new Set();
  for (let e of found) { e = String(e || "").trim().toLowerCase(); if (EMAIL_RE.test(e) && !blocked.has(e)) seen.add(e); }
  return [...seen];
}

async function productEmailHtml(env, base, product, message, email) {
  const link = product ? `${base}/addons/view?id=${encodeURIComponent(product.id)}` : base;
  const unsub = `${base}/unsubscribe?email=${encodeURIComponent(email)}&token=${await unsubscribeToken(env, email)}`;
  const text = String(message || "").split(/\r?\n/).map(escHtml).join("<br>");
  let block = "";
  if (product) {
    const img = /^https:\/\//.test(product.image_url || "") ? `<img src="${escHtml(product.image_url)}" alt="" width="560" style="display:block;width:100%;max-width:560px;height:auto;margin:0 0 20px">` : "";
    block = `${img}<h2 style="margin:0 0 6px;font-size:22px;color:#141517">${escHtml(product.name)}</h2><p style="margin:0 0 6px;color:#6b7480">${escHtml(product.tagline || "")}</p><p style="margin:0 0 22px;font-weight:600;color:#141517">${escHtml(product.price || "")}</p>`;
  }
  return `<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#39424e">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#141517;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">${text}</td></tr>
<tr><td style="padding:12px 28px 28px">${block}<a href="${escHtml(link)}" style="display:inline-block;background:#141517;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">${product ? "View " + escHtml(product.name) : "Visit rotaidem.com"}</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e2e5e9;font-size:12px;color:#6b7480">You are receiving this because you have an account with Rotaidem, contacted us or subscribed to our news.
<a href="${escHtml(unsub)}" style="color:#6b7480">Unsubscribe</a></td></tr>
</table></td></tr></table></body></html>`;
}

async function adminAudience(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const body = await request.json();
  const counts = {};
  for (const g of AUDIENCES) counts[g] = (await collectRecipients(env, [g], "")).length;
  const groups = (body.groups || []).filter(g => AUDIENCES.includes(g));
  return json({ counts, total: (await collectRecipients(env, groups, body.extra)).length, email_ready: !!resendKey(env), from: mailFrom(env) });
}

async function adminBroadcast(request, env) {
  const user = await currentUser(request, env);
  if (!user || !["Owner", "Admin"].includes(user.role)) return json({ error: "Only the Owner or an Admin can send emails" }, 403);
  if (!resendKey(env)) return json({ error: "Email is not set up. Add RESEND_API_KEY to the environment variables and redeploy." }, 400);
  const body = await request.json();
  const subject = String(body.subject || "").trim();
  const message = String(body.message || "").trim();
  if (!subject || !message) return json({ error: "Subject and message are required" }, 400);
  let product = null;
  if (body.product_id) {
    product = await env.DB.prepare("SELECT * FROM products WHERE id = ?").bind(body.product_id).first();
    if (!product) return json({ error: "That product no longer exists" }, 400);
  }
  const test = String(body.test_email || "").trim().toLowerCase();
  if (test && !EMAIL_RE.test(test)) return json({ error: "Enter a valid test address" }, 400);
  const groups = (body.groups || []).filter(g => AUDIENCES.includes(g));
  const recipients = test ? [test] : await collectRecipients(env, groups, body.extra);
  if (!recipients.length) return json({ error: "No recipients match the chosen audience" }, 400);
  const base = (env.SITE_URL || new URL(request.url).origin).replace(/\/$/, "");
  let sent = 0, failed = 0, lastError = "";
  for (let i = 0; i < recipients.length; i += 100) {
    const chunk = recipients.slice(i, i + 100);
    const messages = [];
    for (const r of chunk) messages.push({ from: mailFrom(env), to: [r], subject, html: await productEmailHtml(env, base, product, message, r) });
    const res = await fetch("https://api.resend.com/emails/batch", { method: "POST", headers: { Authorization: `Bearer ${resendKey(env)}`, "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(messages) });
    if (res.ok) sent += chunk.length; else { failed += chunk.length; lastError = explainResendError(env, res.status, await res.text()); }
  }
  if (!test) {
    await env.DB.prepare("INSERT INTO campaigns (subject, product_id, audience, sent_count, failed_count, admin_id) VALUES (?, ?, ?, ?, ?, ?)").bind(subject, product ? product.id : "", groups.join(",") + (body.extra ? " +extra" : ""), sent, failed, user.id).run();
    await audit(env, user, "send_email", `${subject} (${sent} sent)`);
  }
  if (failed && !sent) return json({ error: lastError }, 502);
  return json({ ok: true, sent, failed, test: !!test, error: lastError });
}

async function adminCampaigns(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT campaigns.*, users.name AS admin_name FROM campaigns LEFT JOIN users ON users.id = campaigns.admin_id ORDER BY campaigns.id DESC LIMIT 50").all();
  return json({ campaigns: rows.results || [] });
}

async function adminSubscribers(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = await env.DB.prepare("SELECT * FROM subscribers ORDER BY id DESC").all();
  return json({ subscribers: rows.results || [] });
}

async function adminDeleteSubscriber(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || !["Owner", "Admin"].includes(user.role)) return json({ error: "Only the Owner or an Admin can remove subscribers" }, 403);
  await env.DB.prepare("DELETE FROM subscribers WHERE id = ?").bind(id).run();
  await audit(env, user, "delete_subscriber", id);
  return json({ ok: true });
}

function withImages(item) {
  return { ...item, images: listImages(item.images), features: listImages(item.features), page: "/addons/view?id=" + item.id };
}
function listImages(value) {
  if (Array.isArray(value)) return value.map(String).map(s => s.trim()).filter(Boolean);
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    if (Array.isArray(parsed)) return parsed.map(String).map(s => s.trim()).filter(Boolean);
  } catch (err) {}
  return String(value).split(/\n+/).map(s => s.trim()).filter(Boolean);
}
function bearerToken(request) { return (request.headers.get("Authorization") || "").replace("Bearer ", "").trim(); }
async function currentUser(request, env) {
  const token = bearerToken(request);
  if (!token || !env.DB) return null;
  return env.DB.prepare("SELECT * FROM users WHERE token = ?").bind(token).first();
}
function slug(value) { return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || crypto.randomUUID().slice(0, 8); }
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
  return btoa(String.fromCharCode(...new Uint8Array(bits))) === hashB64;
}
function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "access-control-allow-origin": "*", "access-control-allow-headers": "Content-Type, Authorization", "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS" } });
}

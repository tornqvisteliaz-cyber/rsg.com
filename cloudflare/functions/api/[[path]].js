// A locked staff member (admin password not entered) is treated as a customer by currentUser();
// their "admin only" answers then become "enter the admin password" so the admin page can ask.
const LOCKED = new WeakSet();
export async function onRequest(context) {
  const response = await handleRequest(context);
  if (LOCKED.has(context.request) && (response.status === 401 || response.status === 403))
    return json({ error: "Enter the admin password to continue.", locked: true }, 403);
  return response;
}

async function handleRequest(context) {
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
    if (method === "GET" && path === "/api/admin/gate") return adminGateState(request, env);
    if (method === "POST" && path === "/api/admin/unlock") return adminUnlock(request, env);
    if (method === "POST" && path === "/api/admin/gate") return adminSetGate(request, env);
    if (method === "POST" && path === "/api/password/forgot") return passwordForgot(request, env);
    if (method === "POST" && path === "/api/password/check") return passwordCheck(request, env);
    if (method === "POST" && path === "/api/password/reset") return passwordReset(request, env);
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
    if (method === "GET" && path === "/api/licenses/jwks.json") return json({ keys: [await signingPublicJwk(env)] });
    if (method === "GET" && path === "/api/licenses/public-key") return licensePublicKeyPem(env);
    if (method === "POST" && path === "/api/licenses/activate") return licenseActivate(request, env);
    if (method === "POST" && path === "/api/licenses/validate") return licenseValidate(request, env);
    if (method === "POST" && path === "/api/licenses/deactivate") return licenseDeactivate(request, env);
    if (method === "GET" && path === "/api/account/licenses") return accountLicenses(request, env);
    if (method === "POST" && path === "/api/admin/licenses/generate") return adminGenerateLicenses(request, env);
    if (method === "GET" && path === "/api/admin/licenses") return adminLicenses(request, env);
    if (method === "GET" && /^\/api\/admin\/licenses\/\d+$/.test(path)) return adminLicenseDetail(request, env, Number(path.split("/")[4]));
    if (method === "POST" && /^\/api\/admin\/licenses\/\d+$/.test(path)) return adminUpdateLicense(request, env, Number(path.split("/")[4]));
    if (method === "POST" && /^\/api\/admin\/licenses\/\d+\/email$/.test(path)) return adminEmailLicense(request, env, Number(path.split("/")[4]));
    if (method === "DELETE" && /^\/api\/admin\/licenses\/\d+\/activations\/\d+$/.test(path)) return adminResetActivation(request, env, Number(path.split("/")[4]), Number(path.split("/")[6]));
    if (method === "POST" && path === "/api/subscribe") return subscribe(request, env);
    if (method === "POST" && path === "/api/unsubscribe") return unsubscribe(request, env);
    if (method === "POST" && path === "/api/admin/audience") return adminAudience(request, env);
    if (method === "POST" && path === "/api/admin/broadcast") return adminBroadcast(request, env);
    if (method === "GET" && path === "/api/admin/campaigns") return adminCampaigns(request, env);
    if (method === "GET" && path === "/api/admin/subscribers") return adminSubscribers(request, env);
    if (method === "DELETE" && path.startsWith("/api/admin/subscribers/")) return adminDeleteSubscriber(request, env, Number(path.split("/").pop()));
    if (method === "GET" && path === "/api/site") return json(await siteSettings(env));
    if (method === "GET" && path === "/api/admin/settings") return adminSettings(request, env);
    if (method === "POST" && path === "/api/admin/settings") return adminSaveSettings(request, env);
    if (method === "GET" && path === "/api/admin/media") return adminMedia(request, env);
    if (method === "POST" && path === "/api/admin/media") return adminUploadMedia(request, env);
    if (method === "DELETE" && /^\/api\/admin\/media\/\d+$/.test(path)) return adminDeleteMedia(request, env, Number(path.split("/").pop()));
    if (method === "GET" && /^\/api\/admin\/users\/\d+$/.test(path)) return adminUserDetail(request, env, Number(path.split("/").pop()));
    if (method === "DELETE" && /^\/api\/admin\/users\/\d+$/.test(path)) return adminDeleteUser(request, env, Number(path.split("/").pop()));
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
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS license_failures (id INTEGER PRIMARY KEY AUTOINCREMENT, ip_address TEXT, created_at TEXT)").run();

  for (const col of ["tagline TEXT", "features TEXT", "sort_order INTEGER DEFAULT 0"]) {
    try { await env.DB.prepare("ALTER TABLE products ADD COLUMN " + col).run(); } catch (err) {}
  }
  try { await env.DB.prepare("ALTER TABLE orders ADD COLUMN product_id TEXT").run(); } catch (err) {}
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS licenses (id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE, product_id TEXT, email TEXT, customer_id INTEGER, order_id INTEGER, status TEXT DEFAULT 'active', max_activations INTEGER DEFAULT 2, note TEXT, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS license_activations (id INTEGER PRIMARY KEY AUTOINCREMENT, license_id INTEGER, machine_id TEXT, machine_name TEXT, first_seen TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE (license_id, machine_id))").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, source TEXT, unsubscribed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS media (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, mime TEXT, data TEXT, size INTEGER DEFAULT 0, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, product_id TEXT, audience TEXT, sent_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0, admin_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP)").run();
  // Never delete data here: this runs on every request that starts a new worker.
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
  if (!EMAIL_RE.test(email)) return json({ error: "Enter a valid email address" }, 400);
  const token = crypto.randomUUID();
  try {
    await env.DB.prepare("INSERT INTO users (name, email, password_hash, role, token) VALUES (?, ?, ?, 'Customer', ?)").bind(name, email, await hashPassword(password), token).run();
  } catch { return json({ error: "Email already exists" }, 400); }
  await sendWelcomeEmail(request, env, name, email);
  return json({ token, name, email, role: "Customer", is_admin: false });
}

// Best effort: a failed welcome email never stops the sign-up. Same text as server.py.
async function sendWelcomeEmail(request, env, name, email) {
  if (!resendKey(env)) return;
  const base = (env.SITE_URL || new URL(request.url).origin).replace(/\/$/, "");
  const link = (path, text) => `<a href="${escHtml(base + path)}" style="color:#111315">${escHtml(text)}</a>`;
  const html = `<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">
<h1 style="margin:0 0 12px;font-size:22px;color:#111315">Welcome to Rotaidem, ${escHtml(name)}</h1>
<p style="margin:0 0 16px">Your account is ready. You log in with <strong>${escHtml(email)}</strong> and the password you chose.</p>
<ul style="margin:0 0 16px;padding-left:20px">
<li>Your licence keys, downloads and orders are under ${link("/account", "My account")}.</li>
<li>Installers and updates are on ${link("/downloads", "Downloads")}.</li>
<li>Forgot your password? Use ${link("/forgot-password", "Forgot password")} on the log-in page.</li>
</ul></td></tr>
<tr><td style="padding:4px 28px 28px"><a href="${escHtml(base)}/account" style="display:inline-block;background:#111315;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">Go to my account</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Did not create this account? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a> and we remove it.</td></tr>
</table></td></tr></table></body></html>`;
  try {
    const res = await fetch("https://api.resend.com/emails/batch", {
      method: "POST",
      headers: { Authorization: `Bearer ${resendKey(env)}`, "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify([{ from: mailFrom(env), to: [email], subject: "Welcome to Rotaidem", html }]),
    });
    if (!res.ok) console.error("Welcome email failed:", explainResendError(env, res.status, await res.text()));
  } catch (err) { console.error("Welcome email failed:", err); }
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
  return json({ id: user.id, name: user.name, role: user.role, products: prodCount?.n || 0, customers: custCount?.n || 0, orders: orderCount?.n || 0, posts: postCount?.n || 0, messages: msgCount?.n || 0, subscribers: subCount?.n || 0, licenses: licCount?.n || 0, revenue: Number(revenue?.n || 0), email_ready: !!resendKey(env) });
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
    const email = customer ? customer.email : "";
    if (body.send_email !== false && email) {
      const error = await emailLicenseKeys(env, request, email, [key], body.product_id);
      if (!error) await audit(env, user, "email_licenses", email);
      return json({ ok: true, license_key: key, emailed: error ? [] : [email], email_error: error });
    }
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

// ---------- site settings & media (same rules as server.py) ----------
// Image slots on the public pages. Defaults are the artwork in cloudflare/public/img.
const IMAGE_SLOTS = { hero: "/img/hero.jpg", feature: "/img/approach.jpg", heavypro: "/img/clouds.jpg", header: "/img/night.jpg", login: "/img/clouds.jpg", about: "/img/approach.jpg", product: "/img/approach.jpg" };
const MEDIA_TYPES = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif" };
const isManager = user => user && ["Owner", "Admin"].includes(user.role);
const mediaUrl = row => `/media/${row.id}.${MEDIA_TYPES[row.mime] || "jpg"}`;

async function getSetting(env, key, fallback) {
  const row = await env.DB.prepare("SELECT value FROM meta WHERE key = ?").bind(key).first();
  try { return row ? JSON.parse(row.value) : fallback; } catch (err) { return fallback; }
}
async function setSetting(env, key, value) {
  await env.DB.prepare("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(key, JSON.stringify(value)).run();
}
async function siteSettings(env) {
  const saved = (await getSetting(env, "site.images", {})) || {};
  const images = { ...IMAGE_SLOTS };
  for (const [k, v] of Object.entries(saved)) if (k in IMAGE_SLOTS && v) images[k] = v;
  const banner = (await getSetting(env, "site.announcement", {})) || {};
  const texts = (await getSetting(env, "site.texts", {})) || {};
  return { images, defaults: IMAGE_SLOTS, texts: typeof texts === "object" && !Array.isArray(texts) ? texts : {}, announcement: { enabled: !!banner.enabled, text: String(banner.text || ""), link: String(banner.link || "") } };
}
function validImageUrl(url) {
  url = String(url || "").trim();
  return /^https:\/\//.test(url) || url.startsWith("/img/") || /^\/media\/\d+\.(jpg|png|webp|gif)$/.test(url) ? url : "";
}

async function adminSettings(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  return json(await siteSettings(env));
}

async function adminSaveSettings(request, env) {
  const user = await currentUser(request, env);
  if (!isManager(user)) return json({ error: "Only the Owner or an Admin can change site settings" }, 403);
  const body = await request.json();
  if ("images" in body) {
    const images = {};
    for (const [slot, url] of Object.entries(body.images || {})) {
      if (!(slot in IMAGE_SLOTS) || !url) continue;
      const clean = validImageUrl(url);
      if (!clean) return json({ error: `The ${slot} image must be an https:// link or an uploaded image` }, 400);
      if (clean !== IMAGE_SLOTS[slot]) images[slot] = clean;
    }
    await setSetting(env, "site.images", images);
  }
  if ("announcement" in body) {
    const a = body.announcement || {};
    const link = String(a.link || "").trim();
    if (link && !(link.startsWith("https://") || link.startsWith("/"))) return json({ error: "The banner link must start with https:// or /" }, 400);
    await setSetting(env, "site.announcement", { enabled: !!a.enabled, text: String(a.text || "").slice(0, 200), link: link.slice(0, 300) });
  }
  if ("texts" in body) {
    // A patch: {"page.key": "new text"} changes a text, null or "" puts the original back.
    const changes = body.texts;
    if (!changes || typeof changes !== "object" || Array.isArray(changes)) return json({ error: "texts must be an object" }, 400);
    const texts = { ...((await getSetting(env, "site.texts", {})) || {}) };
    for (const [key, raw] of Object.entries(changes)) {
      if (!/^[a-z0-9-]{1,40}\.[a-z0-9-]{1,60}$/.test(key)) return json({ error: `Unknown text ${key}` }, 400);
      const value = raw == null ? "" : String(raw).replace(/\r\n/g, "\n").trim();
      if (value.length > 5000) return json({ error: "A text can be at most 5000 characters" }, 400);
      if (value) texts[key] = value; else delete texts[key];
    }
    if (Object.keys(texts).length > 1000) return json({ error: "Too many changed texts" }, 400);
    await setSetting(env, "site.texts", texts);
  }
  await audit(env, user, "save_settings", ["images", "announcement", "texts"].filter(k => k in body).join(", ")
    + (body.texts && typeof body.texts === "object" ? ": " + Object.keys(body.texts).slice(0, 5).join(", ") : ""));
  return json({ ok: true, ...(await siteSettings(env)) });
}

async function adminMedia(request, env) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const rows = (await env.DB.prepare("SELECT id, filename, mime, size, width, height, created_at FROM media ORDER BY id DESC").all()).results || [];
  return json({ media: rows.map(r => ({ ...r, url: mediaUrl(r) })) });
}

async function adminUploadMedia(request, env) {
  const user = await currentUser(request, env);
  if (!isManager(user)) return json({ error: "Only the Owner or an Admin can upload images" }, 403);
  const body = await request.json();
  const mime = String(body.mime || "");
  if (!MEDIA_TYPES[mime]) return json({ error: "Upload a JPG, PNG, WebP or GIF image" }, 400);
  let data = String(body.data || "");
  if (data.startsWith("data:") && data.includes(",")) data = data.slice(data.indexOf(",") + 1);
  let size;
  try { size = atob(data).length; } catch (err) { return json({ error: "The image data could not be read" }, 400); }
  // D1 stores at most about 2 MB per value, and base64 adds a third.
  if (!size || size > 1400 * 1024) return json({ error: "Images must be smaller than 1.4 MB on this host" }, 400);
  const name = String(body.filename || "image").replace(/[^\w.\- ]/g, "").slice(0, 120) || "image";
  const row = await env.DB.prepare("INSERT INTO media (filename, mime, data, size, width, height) VALUES (?, ?, ?, ?, ?, ?) RETURNING id")
    .bind(name, mime, data, size, clampInt(body.width, 0, 0, 20000), clampInt(body.height, 0, 0, 20000)).first();
  await audit(env, user, "upload_media", name);
  return json({ ok: true, id: row.id, url: mediaUrl({ id: row.id, mime }) });
}

async function adminDeleteMedia(request, env, id) {
  const user = await currentUser(request, env);
  if (!isManager(user)) return json({ error: "Only the Owner or an Admin can delete images" }, 403);
  const row = await env.DB.prepare("SELECT filename FROM media WHERE id = ?").bind(id).first();
  await env.DB.prepare("DELETE FROM media WHERE id = ?").bind(id).run();
  await audit(env, user, "delete_media", row ? row.filename : id);
  return json({ ok: true });
}

async function adminUserDetail(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role === "Customer") return json({ error: "Admin only" }, 403);
  const u = await env.DB.prepare("SELECT id, name, email, role, created_at FROM users WHERE id = ?").bind(id).first();
  if (!u) return json({ error: "User not found" }, 404);
  u.orders = (await env.DB.prepare("SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC").bind(id).all()).results || [];
  u.licenses = (await env.DB.prepare("SELECT licenses.*, (SELECT COUNT(*) FROM license_activations a WHERE a.license_id = licenses.id) AS activations FROM licenses WHERE customer_id = ? OR LOWER(email) = ? ORDER BY id DESC")
    .bind(id, String(u.email || "").toLowerCase()).all()).results || [];
  return json({ user: u });
}

async function adminDeleteUser(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || user.role !== "Owner") return json({ error: "Only the Owner can delete users" }, 403);
  if (id === user.id) return json({ error: "You cannot delete your own account" }, 400);
  const target = await env.DB.prepare("SELECT email FROM users WHERE id = ?").bind(id).first();
  if (!target) return json({ error: "User not found" }, 404);
  // Licences and orders stay (they are records of a sale); they are just no longer linked to an account.
  await env.DB.prepare("UPDATE licenses SET customer_id = NULL WHERE customer_id = ?").bind(id).run();
  await env.DB.prepare("DELETE FROM users WHERE id = ?").bind(id).run();
  await audit(env, user, "delete_user", target.email);
  return json({ ok: true });
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

// Same rule as same_product() in server.py: ignore case/punctuation, and let one id be the other plus whole extra words.
function sameProduct(requested, stored) {
  const parts = v => { const w = String(v || "").toLowerCase().match(/[a-z0-9]+/g) || []; return { all: new Set(w.map((_, i) => w.slice(0, i + 1).join(""))), full: w.join("") }; };
  const a = parts(requested), b = parts(stored);
  return !!a.full && !!b.full && (b.all.has(a.full) || a.all.has(b.full));
}

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

function licenseEmailHtml(base, keys, productName, max) {
  const rows = keys.map(k => `<tr><td style="padding:14px 18px;background:#f4f4f5;border:1px solid #e4e4e7;font:bold 20px/1.2 Consolas,Menlo,monospace;letter-spacing:2px;color:#111315;text-align:center">${escHtml(k)}</td></tr><tr><td style="height:8px"></td></tr>`).join("");
  const plural = keys.length > 1;
  return `<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 6px;font-size:16px;line-height:1.6">
<h1 style="margin:0 0 12px;font-size:22px;color:#111315">Your ${escHtml(productName)} licence key${plural ? "s" : ""}</h1>
<p style="margin:0 0 20px">Thank you for choosing ${escHtml(productName)}. Here ${plural ? "are your keys" : "is your key"}:</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0">${rows}</table></td></tr>
<tr><td style="padding:12px 28px 8px;font-size:15px;line-height:1.6">
<h2 style="margin:0 0 8px;font-size:17px;color:#111315">How to activate</h2>
<ol style="margin:0 0 16px;padding-left:20px">
<li>Download the installer from <a href="${escHtml(base)}/downloads" style="color:#111315">${escHtml(base.split("://").pop())}/downloads</a>.</li>
<li>Run it and enter your key when asked.</li>
<li>Each key works on ${max} computer${max !== 1 ? "s" : ""}. To move to a new PC, deactivate it in ${escHtml(productName)} first.</li>
</ol></td></tr>
<tr><td style="padding:4px 28px 28px"><a href="${escHtml(base)}/account" style="display:inline-block;background:#111315;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">View my keys</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Keep this email somewhere safe. Questions? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a>.</td></tr>
</table></td></tr></table></body></html>`;
}

// Returns an error message, or "" when the email was sent.
async function emailLicenseKeys(env, request, email, keys, productId) {
  if (!keys.length || !email) return "";
  if (!resendKey(env)) return "Email is not set up (RESEND_API_KEY is missing), so the key was not emailed.";
  const product = await env.DB.prepare("SELECT name FROM products WHERE id = ?").bind(productId).first();
  const name = product ? product.name : (productId === "heavypro" ? "HeavyPro" : productId);
  const lic = await env.DB.prepare("SELECT max_activations FROM licenses WHERE license_key = ?").bind(keys[0]).first();
  const base = (env.SITE_URL || new URL(request.url).origin).replace(/\/$/, "");
  const res = await fetch("https://api.resend.com/emails/batch", {
    method: "POST",
    headers: { Authorization: `Bearer ${resendKey(env)}`, "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify([{ from: mailFrom(env), to: [email], subject: `Your ${name} licence key${keys.length > 1 ? "s" : ""}`, html: licenseEmailHtml(base, keys, name, lic ? lic.max_activations : 2) }]),
  });
  return res.ok ? "" : explainResendError(env, res.status, await res.text());
}

async function adminEmailLicense(request, env, id) {
  const user = await currentUser(request, env);
  if (!user || !["Owner", "Admin"].includes(user.role)) return json({ error: "Only the Owner or an Admin can email licences" }, 403);
  const lic = await env.DB.prepare("SELECT * FROM licenses WHERE id = ?").bind(id).first();
  if (!lic) return json({ error: "Licence not found" }, 404);
  const body = await request.json().catch(() => ({}));
  const email = String(body.email || lic.email || "").trim().toLowerCase();
  if (!EMAIL_RE.test(email)) return json({ error: "This licence has no valid customer email. Add one first." }, 400);
  const error = await emailLicenseKeys(env, request, email, [lic.license_key], lic.product_id);
  if (error) return json({ error }, 502);
  await audit(env, user, "email_licenses", `${lic.license_key} to ${email}`);
  return json({ ok: true, emailed: [email] });
}

async function licensesImport(request, env) {
  const user = await licenseToolUser(request, env);
  if (!user) return json({ error: "Not allowed. Check the generator's API key." }, 403);
  const payload = await request.json();
  const items = payload.licenses || [];
  if (!Array.isArray(items) || items.length > 1000) return json({ error: "Send a list of at most 1000 licences" }, 400);
  const created = [], skipped = [], invalid = [];
  const byEmail = new Map();
  for (const item of items) {
    const key = normalizeKey(item.key);
    if (!key) { invalid.push(String(item.key || "").slice(0, 40)); continue; }
    const email = String(item.email || "").trim().toLowerCase();
    const ok = await insertLicense(env, key, String(item.product_id || "heavypro").slice(0, 60), email, await customerIdFor(env, email), null, clampInt(item.max_activations, 2, 1, 100), String(item.note || "").slice(0, 200), user.name);
    (ok ? created : skipped).push(key);
    if (ok && EMAIL_RE.test(email)) {
      const group = email + "|" + String(item.product_id || "heavypro").slice(0, 60);
      byEmail.set(group, (byEmail.get(group) || []).concat(key));
    }
  }
  if (created.length) await audit(env, user, "import_licenses", `${created.length} keys`);
  const emailed = [], errors = [];
  if (payload.send_email) {
    for (const [group, keys] of byEmail) {
      const [email, productId] = group.split("|");
      const error = await emailLicenseKeys(env, request, email, keys, productId);
      if (error) errors.push(`${email}: ${error}`); else emailed.push(email);
    }
    if (emailed.length) await audit(env, user, "email_licenses", emailed.join(", "));
  }
  return json({ ok: true, created, skipped, invalid, emailed, email_error: errors.join("; ") });
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
  const send = email && body.send_email !== false;
  const error = send ? await emailLicenseKeys(env, request, email, keys, String(body.product_id || "heavypro").slice(0, 60)) : "";
  if (send && !error) await audit(env, user, "email_licenses", email);
  return json({ ok: true, keys, emailed: send && !error ? [email] : [], email_error: error });
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
  const productId = String(body.product_id || lic.product_id || "heavypro").trim().slice(0, 60);
  await env.DB.prepare("UPDATE licenses SET status = ?, email = ?, customer_id = ?, max_activations = ?, note = ?, product_id = ? WHERE id = ?")
    .bind(status, email, await customerIdFor(env, email), clampInt(body.max_activations, lic.max_activations, 1, 100), String(body.note ?? lic.note ?? "").slice(0, 200), productId, id).run();
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

// ---------- signed licence answers (same scheme as server.py) ----------
// Valid answers carry an RS256 token signed with the key in meta "license_signing_key" (hex JSON
// {n, e, d, p, q}, the same format server.py uses, so the key moves with backups).
const LICENSE_LEASE_DAYS = 14, LICENSE_FAILURES_PER_HOUR = 30;
const b64urlBytes = bytes => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const b64urlText = text => b64urlBytes(new TextEncoder().encode(text));
const bytesFromB64url = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0));
const bigFromBytes = bytes => BigInt("0x" + ([...bytes].map(b => b.toString(16).padStart(2, "0")).join("") || "0"));
const bytesFromBig = n => { let h = n.toString(16); if (h.length % 2) h = "0" + h; return Uint8Array.from(h.match(/../g), x => parseInt(x, 16)); };
function modInverse(a, m) {
  let [r0, r1, s0, s1] = [m, ((a % m) + m) % m, 0n, 1n];
  while (r1) { const q = r0 / r1; [r0, r1] = [r1, r0 - q * r1]; [s0, s1] = [s1, s0 - q * s1]; }
  return ((s0 % m) + m) % m;
}
async function signingKeyParts(env) {
  let row = await env.DB.prepare("SELECT value FROM meta WHERE key = 'license_signing_key'").first();
  if (!row) {
    const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
    const jwk = await crypto.subtle.exportKey("jwk", pair.privateKey);
    const hex = {}; for (const k of ["n", "e", "d", "p", "q"]) hex[k] = bigFromBytes(bytesFromB64url(jwk[k])).toString(16);
    await env.DB.prepare("INSERT OR IGNORE INTO meta (key, value) VALUES ('license_signing_key', ?)").bind(JSON.stringify(hex)).run();
    row = await env.DB.prepare("SELECT value FROM meta WHERE key = 'license_signing_key'").first();
  }
  const parts = {}; for (const [k, v] of Object.entries(JSON.parse(row.value))) parts[k] = BigInt("0x" + v);
  return parts;
}
async function signingKeyId(parts) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytesFromBig(parts.n)));
  return [...digest].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}
async function signingPublicJwk(env) {
  const k = await signingKeyParts(env);
  return { kty: "RSA", alg: "RS256", use: "sig", kid: await signingKeyId(k), n: b64urlBytes(bytesFromBig(k.n)), e: b64urlBytes(bytesFromBig(k.e)) };
}
async function licensePublicKeyPem(env) {
  const pub = await crypto.subtle.importKey("jwk", { ...(await signingPublicJwk(env)), ext: true }, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, true, ["verify"]);
  const der = btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.exportKey("spki", pub))));
  return new Response(`-----BEGIN PUBLIC KEY-----\n${der.match(/.{1,64}/g).join("\n")}\n-----END PUBLIC KEY-----\n`, { headers: { "content-type": "application/x-pem-file" } });
}
async function makeLicenseToken(env, claims) {
  const k = await signingKeyParts(env);
  const b = n => b64urlBytes(bytesFromBig(n));
  const key = await crypto.subtle.importKey("jwk", { kty: "RSA", n: b(k.n), e: b(k.e), d: b(k.d), p: b(k.p), q: b(k.q), dp: b(k.d % (k.p - 1n)), dq: b(k.d % (k.q - 1n)), qi: b(modInverse(k.q, k.p)), alg: "RS256", ext: true },
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const sorted = Object.fromEntries(Object.entries(claims).sort(([a], [b2]) => (a < b2 ? -1 : 1)));
  const data = b64urlText(JSON.stringify({ alg: "RS256", typ: "JWT", kid: await signingKeyId(k) })) + "." + b64urlText(JSON.stringify(sorted));
  return data + "." + b64urlBytes(new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(data))));
}
const licenseIp = request => request.headers.get("CF-Connecting-IP") || "";
async function licenseFailed(request, env, reply) {
  await env.DB.prepare("INSERT INTO license_failures (ip_address, created_at) VALUES (?, ?)").bind(licenseIp(request), utcStamp()).run();
  return reply;
}

async function licenseForRequest(request, env) {
  const body = await request.json().catch(() => ({}));
  const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM license_failures WHERE ip_address = ? AND created_at >= ?").bind(licenseIp(request), utcStamp(3600)).first();
  if (recent && recent.n >= LICENSE_FAILURES_PER_HOUR) return { error: json({ valid: false, reason: "rate_limited", error: "Too many wrong licence keys from this address. Try again in an hour." }, 429) };
  const key = normalizeKey(body.key);
  if (!key) return { error: await licenseFailed(request, env, json({ valid: false, reason: "invalid_format", error: "That is not a valid licence key." }, 400)) };
  const lic = await env.DB.prepare("SELECT * FROM licenses WHERE license_key = ?").bind(key).first();
  if (!lic) return { error: await licenseFailed(request, env, json({ valid: false, reason: "not_found", error: "This licence key does not exist." }, 404)) };
  if (lic.status !== "active") return { error: json({ valid: false, reason: "revoked", error: "This licence key has been revoked." }, 403) };
  const product = String(body.product || "").trim();
  if (product && !sameProduct(product, lic.product_id)) return { error: json({ valid: false, reason: "wrong_product", error: "This licence key is for another product." }, 403) };
  const machine = String(body.machine_id || "").trim().slice(0, 128);
  if (!machine) return { error: json({ valid: false, reason: "missing_machine", error: "machine_id is required." }, 400) };
  return { lic, machine, body };
}
const activationCount = async (env, id) => ((await env.DB.prepare("SELECT COUNT(*) AS n FROM license_activations WHERE license_id = ?").bind(id).first()) || {}).n || 0;
async function licenseReply(request, env, r, used) {
  const now = Math.floor(Date.now() / 1000), days = Number(env.LICENSE_LEASE_DAYS) || LICENSE_LEASE_DAYS, exp = now + days * 86400;
  const claims = { iss: (env.SITE_URL || new URL(request.url).origin).replace(/\/$/, ""), sub: `license:${r.lic.id}`, aud: r.lic.product_id, key: r.lic.license_key,
    mid: r.machine, iat: now, exp, activations_used: used, max_activations: r.lic.max_activations };
  const nonce = String(r.body.nonce || "").slice(0, 128);
  if (nonce) claims.nonce = nonce;
  const iso = t => new Date(t * 1000).toISOString().replace(/\.\d+Z$/, "Z");
  return json({ valid: true, product_id: r.lic.product_id, activations_used: used, max_activations: r.lic.max_activations,
    checked_at: iso(now), lease_until: iso(exp), token: await makeLicenseToken(env, claims) });
}

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
  return licenseReply(request, env, r, used);
}

async function licenseValidate(request, env) {
  const r = await licenseForRequest(request, env);
  if (r.error) return r.error;
  const existing = await env.DB.prepare("SELECT id FROM license_activations WHERE license_id = ? AND machine_id = ?").bind(r.lic.id, r.machine).first();
  if (!existing) return json({ valid: false, reason: "not_activated", error: "This computer has not been activated with this key." }, 403);
  await env.DB.prepare("UPDATE license_activations SET last_seen = CURRENT_TIMESTAMP WHERE id = ?").bind(existing.id).run();
  return licenseReply(request, env, r, await activationCount(env, r.lic.id));
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

// ---------- admin password ----------
// Same scheme as server.py: only a hash is stored (meta "admin.gate"); the unlock code is signed
// over the staff member's session token and the current admin password hash.
const STAFF_ROLES = ["Owner", "Admin", "Support"];
const GATE_KEY = "admin.gate", GATE_HOURS = 12, GATE_ATTEMPTS = 5, GATE_MINUTES = 15;

async function gateSignature(env, user, expires, stored) {
  const secret = env.SECRET_KEY || env.ADMIN_PASSWORD || "rotaidem-dev-secret";
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`gate:${user.id}:${user.token || ""}:${expires}:${stored}`));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 40);
}
async function gateHash(env) { return String((await getSetting(env, GATE_KEY, "")) || ""); }
async function adminUnlocked(request, env, user) {
  const stored = await gateHash(env);
  if (!stored) return true;
  const [expires, signature] = String(request.headers.get("X-Rotaidem-Admin") || "").trim().split(".");
  return /^\d+$/.test(expires || "") && Number(expires) > Date.now() / 1000 && (await gateSignature(env, user, Number(expires), stored)) === signature;
}
async function unlockCode(env, user, stored) {
  const expires = Math.floor(Date.now() / 1000) + GATE_HOURS * 3600;
  return `${expires}.${await gateSignature(env, user, expires, stored)}`;
}
async function adminGateState(request, env) {
  const user = await currentUser(request, env, { ignoreGate: true });
  if (!user || !STAFF_ROLES.includes(user.role)) return json({ error: "Admin only" }, 403);
  return json({ enabled: !!(await gateHash(env)), unlocked: await adminUnlocked(request, env, user), can_change: user.role === "Owner" });
}
async function adminUnlock(request, env) {
  const user = await currentUser(request, env, { ignoreGate: true });
  if (!user || !STAFF_ROLES.includes(user.role)) return json({ error: "Admin only" }, 403);
  const stored = await gateHash(env);
  if (!stored) return json({ unlock: "", enabled: false });
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const failed = await env.DB.prepare("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'admin_unlock_failed' AND admin_id = ? AND created_at >= ?").bind(user.id, utcStamp(GATE_MINUTES * 60)).first();
  if (failed && failed.n >= GATE_ATTEMPTS) return json({ error: `Too many wrong attempts. Wait ${GATE_MINUTES} minutes and try again.` }, 429);
  const body = await request.json().catch(() => ({}));
  const ok = await verifyPassword(String(body.password || ""), stored);
  await env.DB.prepare("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, ?, '', ?, ?)").bind(user.id, ok ? "admin_unlock" : "admin_unlock_failed", ip, utcStamp()).run();
  if (!ok) return json({ error: "Wrong admin password" }, 401);
  return json({ unlock: await unlockCode(env, user, stored), enabled: true });
}
async function adminSetGate(request, env) {
  const user = await currentUser(request, env);
  if (!user || !STAFF_ROLES.includes(user.role)) return json({ error: "Admin only" }, 403);
  if (user.role !== "Owner") return json({ error: "Only the Owner can change the admin password" }, 403);
  const body = await request.json().catch(() => ({}));
  const stored = await gateHash(env);
  if (stored && !(await verifyPassword(String(body.current_password || ""), stored))) return json({ error: "The current admin password is wrong" }, 400);
  if (body.disable) {
    await env.DB.prepare("DELETE FROM meta WHERE key = ?").bind(GATE_KEY).run();
    await audit(env, user, "admin_password", "removed");
    return json({ ok: true, enabled: false });
  }
  const next = String(body.new_password || "");
  if (next.length < 8) return json({ error: "The admin password needs at least 8 characters" }, 400);
  if (await verifyPassword(next, user.password_hash)) return json({ error: "Use a different password than your own login password" }, 400);
  const hash = await hashPassword(next);
  await setSetting(env, GATE_KEY, hash);
  await audit(env, user, "admin_password", stored ? "changed" : "turned on");
  return json({ ok: true, enabled: true, unlock: await unlockCode(env, user, hash) });
}

// ---------- forgot password ----------
// The reset link carries "user id.expiry.signature". The signature covers the current password
// hash, so a link stops working once the password changes and nothing has to be stored.
// Same format as server.py.
const RESET_HOURS = 1, RESET_PER_ACCOUNT = 3, RESET_PER_IP = 10;
const RESET_EXPIRED = "This link has expired or was already used. Ask for a new one.";
const utcStamp = (secondsAgo = 0) => new Date(Date.now() - secondsAgo * 1000).toISOString().slice(0, 19).replace("T", " ");

async function resetSignature(env, userId, expires, passwordHash) {
  const secret = env.SECRET_KEY || env.ADMIN_PASSWORD || "rotaidem-dev-secret";
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`reset:${userId}:${expires}:${passwordHash || ""}`));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 40);
}

async function userForResetToken(env, token) {
  const parts = String(token || "").trim().split(".");
  if (parts.length !== 3 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return null;
  const [userId, expires, signature] = [Number(parts[0]), Number(parts[1]), parts[2]];
  if (expires < Date.now() / 1000) return null;
  const user = await env.DB.prepare("SELECT * FROM users WHERE id = ?").bind(userId).first();
  if (!user || (await resetSignature(env, userId, expires, user.password_hash)) !== signature) return null;
  return user;
}

function maskEmail(email) {
  const [name, domain] = String(email || "").split("@");
  return domain ? name.slice(0, 1) + "•".repeat(Math.max(1, Math.min(name.length - 1, 6))) + "@" + domain : "";
}

function resetEmailHtml(name, link) {
  return `<!DOCTYPE html><html><body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#3a3c40">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff">
<tr><td style="background:#111315;padding:20px 28px;color:#ffffff;font-weight:bold;letter-spacing:3px;font-size:15px">ROTAIDEM</td></tr>
<tr><td style="padding:28px 28px 8px;font-size:16px;line-height:1.6">
<h1 style="margin:0 0 12px;font-size:22px;color:#111315">Reset your password</h1>
<p style="margin:0 0 12px">Hi ${escHtml(name || "there")},</p>
<p style="margin:0 0 20px">Someone (hopefully you) asked to reset the password of your Rotaidem account. Click the button to choose a new one. The link works for ${RESET_HOURS} hour and only once.</p></td></tr>
<tr><td style="padding:4px 28px 20px"><a href="${escHtml(link)}" style="display:inline-block;background:#111315;color:#ffffff;text-decoration:none;padding:13px 24px;font-weight:bold">Choose a new password</a></td></tr>
<tr><td style="padding:0 28px 24px;font-size:13px;line-height:1.6;color:#6d6f75">If the button does not work, copy this address into your browser:<br><a href="${escHtml(link)}" style="color:#6d6f75;word-break:break-all">${escHtml(link)}</a></td></tr>
<tr><td style="padding:20px 28px;border-top:1px solid #e4e4e7;font-size:12px;color:#6d6f75">Did not ask for this? Then you can ignore this email: your password stays the same. Questions? Email <a href="mailto:Eliaz@rotaidem.com" style="color:#6d6f75">Eliaz@rotaidem.com</a>.</td></tr>
</table></td></tr></table></body></html>`;
}

// Always gives the same answer, so nobody can find out which addresses have an account.
async function passwordForgot(request, env) {
  const body = await request.json().catch(() => ({}));
  const ident = String(body.email || "").trim().toLowerCase();
  if (!ident) return json({ error: "Enter the email address of your account" }, 400);
  if (!resendKey(env)) return json({ error: "Password reset by email is not available right now. Please email Eliaz@rotaidem.com." }, 503);
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const hourAgo = utcStamp(3600);
  const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'password_reset_request' AND ip_address = ? AND created_at >= ?").bind(ip, hourAgo).first();
  if (recent && recent.n >= RESET_PER_IP) return json({ error: "Too many requests. Please wait an hour and try again." }, 429);
  const user = await env.DB.prepare("SELECT * FROM users WHERE LOWER(email) = ? OR LOWER(name) = ?").bind(ident, ident).first();
  await env.DB.prepare("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'password_reset_request', ?, ?, ?)")
    .bind(user ? user.id : null, ident.slice(0, 200), ip, utcStamp()).run();
  const answer = json({ ok: true, message: "If an account exists for that address, we have sent a link to reset the password. Check your inbox (and spam)." });
  if (!user || !EMAIL_RE.test(user.email || "")) return answer;
  const sentLately = await env.DB.prepare("SELECT COUNT(*) AS n FROM audit_logs WHERE action = 'password_reset_request' AND admin_id = ? AND created_at >= ?").bind(user.id, hourAgo).first();
  if (sentLately && sentLately.n > RESET_PER_ACCOUNT) return answer; // enough emails for now; say the same thing
  const expires = Math.floor(Date.now() / 1000) + RESET_HOURS * 3600;
  const token = `${user.id}.${expires}.${await resetSignature(env, user.id, expires, user.password_hash)}`;
  const base = (env.SITE_URL || new URL(request.url).origin).replace(/\/$/, "");
  const link = `${base}/reset-password?token=${encodeURIComponent(token)}`;
  const res = await fetch("https://api.resend.com/emails/batch", {
    method: "POST",
    headers: { Authorization: `Bearer ${resendKey(env)}`, "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify([{ from: mailFrom(env), to: [user.email], subject: "Reset your Rotaidem password", html: resetEmailHtml(user.name, link) }]),
  });
  if (!res.ok) {
    console.error("Password reset email failed:", explainResendError(env, res.status, await res.text()));
    return json({ error: "The email could not be sent right now. Please try again later or email Eliaz@rotaidem.com." }, 502);
  }
  return answer;
}

async function passwordCheck(request, env) {
  const body = await request.json().catch(() => ({}));
  const user = await userForResetToken(env, body.token);
  if (!user) return json({ error: RESET_EXPIRED }, 400);
  return json({ ok: true, email: maskEmail(user.email) });
}

async function passwordReset(request, env) {
  const body = await request.json().catch(() => ({}));
  const user = await userForResetToken(env, body.token);
  if (!user) return json({ error: RESET_EXPIRED }, 400);
  const password = String(body.password || "");
  if (password.length < 6) return json({ error: "The new password must be at least 6 characters" }, 400);
  const token = crypto.randomUUID(); // signs out every other browser and signs this one in
  await env.DB.prepare("UPDATE users SET password_hash = ?, token = ? WHERE id = ?").bind(await hashPassword(password), token, user.id).run();
  await env.DB.prepare("INSERT INTO audit_logs (admin_id, action, target, ip_address, created_at) VALUES (?, 'password_reset', ?, ?, ?)")
    .bind(user.id, user.email || "", request.headers.get("CF-Connecting-IP") || "", utcStamp()).run();
  return json({ token, name: user.name, email: user.email, role: user.role, is_admin: ["Owner", "Admin", "Support"].includes(user.role) });
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
    const src = /^\/(media|img)\//.test(product.image_url || "") ? base + product.image_url : product.image_url || "";
    const img = /^https:\/\//.test(src) ? `<img src="${escHtml(src)}" alt="" width="560" style="display:block;width:100%;max-width:560px;height:auto;margin:0 0 20px">` : "";
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
function bearerToken(request) {
  // X-Rotaidem-Token is what the site sends; Authorization stays supported for other clients.
  return (request.headers.get("Authorization") || "").replace("Bearer ", "").trim() || (request.headers.get("X-Rotaidem-Token") || "").trim();
}
async function currentUser(request, env, { ignoreGate = false } = {}) {
  const token = bearerToken(request);
  if (!token || !env.DB) return null;
  const user = await env.DB.prepare("SELECT * FROM users WHERE token = ?").bind(token).first();
  if (!user || ignoreGate || !STAFF_ROLES.includes(user.role) || (await adminUnlocked(request, env, user))) return user;
  LOCKED.add(request);
  return { ...user, role: "Customer" };
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

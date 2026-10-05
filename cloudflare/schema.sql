CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  email TEXT UNIQUE,
  password_hash TEXT,
  role TEXT DEFAULT 'Customer',
  token TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  name TEXT,
  simulator TEXT,
  version TEXT,
  folder_name TEXT,
  download_url TEXT,
  image_url TEXT,
  price TEXT,
  description TEXT,
  buy_url TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS liveries (
  id TEXT PRIMARY KEY,
  name TEXT,
  aircraft TEXT,
  folder_name TEXT,
  download_url TEXT,
  image_url TEXT
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT,
  content TEXT,
  image_url TEXT,
  date TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  email TEXT,
  message TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

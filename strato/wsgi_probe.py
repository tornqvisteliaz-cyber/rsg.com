"""Calls a cgi-bin entry script the way STRATO's new platform does, for diagnose and the tests.

STRATO's FastCGI launcher (cm4all, flup) imports the .py file once as a module named after the
file and calls its `application(environ, start_response)` for each request. This does the same in
one process, so several requests share the loaded app just like on the server.

usage: python3 wsgi_probe.py /path/to/cgi-bin/rotaidem.py < requests.json
       (a JSON list such as [{"path": "/health"}], read from standard input)
Each request may have: method, path, query, body (JSON value or string), headers (dict),
redirect_auth (bool: put Authorization in REDIRECT_HTTP_AUTHORIZATION, as mod_rewrite does),
no_path_info (bool: leave PATH_INFO empty, as some FastCGI setups do).
Prints a JSON list of {"status", "headers", "body"}.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import sys


def load(path):
    name = os.path.splitext(os.path.basename(path))[0]
    loader = importlib.machinery.SourceFileLoader(name, path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module.application


def call(application, script, req):
    body = req.get("body", b"")
    if not isinstance(body, (bytes, str)):
        body = json.dumps(body)
    body = body.encode() if isinstance(body, str) else body
    path, query = req.get("path", "/"), req.get("query", "")
    script_name = "/cgi-bin/" + os.path.basename(script)
    env = {"REQUEST_METHOD": req.get("method", "GET"), "SCRIPT_NAME": script_name, "SCRIPT_FILENAME": script,
           "PATH_INFO": "" if req.get("no_path_info") else path, "QUERY_STRING": query,
           "REQUEST_URI": (path if not path.startswith("/cgi-bin/") else path) + (f"?{query}" if query else ""),
           "SERVER_NAME": "rotaidem.test", "SERVER_PORT": "443", "SERVER_PROTOCOL": "HTTP/1.1", "HTTPS": "on",
           "REMOTE_ADDR": "203.0.113.5", "HTTP_HOST": "rotaidem.test", "GATEWAY_INTERFACE": "CGI/1.1",
           "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "wsgi.errors": sys.stderr,
           "wsgi.version": (1, 0), "wsgi.url_scheme": "https", "wsgi.multithread": True,
           "wsgi.multiprocess": True, "wsgi.run_once": False}
    if body:
        env["CONTENT_TYPE"] = "application/json"
    for key, value in (req.get("headers") or {}).items():
        name = key.upper().replace("-", "_")
        if name == "AUTHORIZATION" and req.get("redirect_auth"):
            env["REDIRECT_HTTP_AUTHORIZATION"] = value
        else:
            env["HTTP_" + name] = value
    started = {}

    def start_response(status, headers, exc_info=None):
        started["status"], started["headers"] = status, headers

    chunks = application(env, start_response)
    data = b"".join(chunks)
    if hasattr(chunks, "close"):
        chunks.close()
    return {"status": int(started["status"].split()[0]), "headers": {k.lower(): v for k, v in started["headers"]},
            "body": data.decode("utf-8", "replace")}


def main():
    script, requests = sys.argv[1], json.load(sys.stdin)
    application = load(script)
    print(json.dumps([call(application, script, r) for r in requests]))


if __name__ == "__main__":
    main()

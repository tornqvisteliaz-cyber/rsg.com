from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "RSG is online"

@app.route("/health")
def health():
    return "OK"
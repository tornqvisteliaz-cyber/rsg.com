# Entry point for Python hosts (Starto, Docker, Procfile): gunicorn main:app
from server import app

if __name__ == "__main__":
    app.run()

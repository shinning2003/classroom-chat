"""Launch the Campus Whispers server locally."""
import os
from app import create_app, init_db

if __name__ == "__main__":
    db_path = os.environ.get("DB_PATH", "campus_whispers.db")
    app = create_app({"DB_PATH": db_path})
    with app.app_context():
        init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

"""Campus Whispers — accountability board for a class.

Posters register a REAL NAME + EMAIL + PASSWORD, choose a public HANDLE.
The handle is shown publicly; only the admin (owner) can see the real
identity behind a handle, and can ban/delete anyone who misbehaves.
"""
import os
import re
import socket
import sqlite3
import threading
import time
import json
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, request, session, current_app, g
from werkzeug.security import generate_password_hash, check_password_hash


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DB_PATH=os.environ.get("DB_PATH", "campus_whispers.db"),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin123"),
        ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL", "11surendiran2003@gmail.com"),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        # Web Push (VAPID). Keys are base64url; set on Render via env vars.
        # When unset, push is disabled and the app degrades gracefully.
        VAPID_PUBLIC_KEY=os.environ.get("VAPID_PUBLIC_KEY", ""),
        VAPID_PRIVATE_KEY=os.environ.get("VAPID_PRIVATE_KEY", ""),
        VAPID_SUBJECT=os.environ.get("VAPID_SUBJECT",
                                     "mailto:11surendiran2003@gmail.com"),
        SESSION_PERMANENT=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if config:
        app.config.update(config)

    @app.before_request
    def persist_session():
        session.permanent = True

    @app.before_request
    def block_banned_users():
        # A ban must kill the account immediately, not just at next login:
        # any authenticated API request from a removed account is rejected.
        uid = session.get("user_id")
        if not uid or not request.path.startswith("/api/"):
            return
        conn = get_db()  # cached per request; teardown closes it
        row = exec(conn, "SELECT banned FROM users WHERE id=?",
                   (uid,)).fetchone()
        if row is None or row["banned"]:
            session.pop("user_id", None)
            return jsonify({"error": "Account removed."}), 403

    @app.teardown_appcontext
    def close_db(exc=None):
        db = g.pop("db", None)
        if db is not None and not getattr(db, "_orig_close", False):
            db.close()  # SQLite connections need explicit close

    @app.post("/api/register")
    def register():
        p = request.get_json(silent=True) or {}
        real_name = (p.get("real_name") or "").strip()
        email = (p.get("email") or "").strip().lower()
        handle = (p.get("handle") or "").strip()
        password = p.get("password") or ""
        if not (real_name and email and handle and password):
            return jsonify({"error": "real_name, email, handle and password required."}), 400
        if len(password) < 4:
            return jsonify({"error": "Password too short (min 4)."}), 400
        if not re.match(r"^[A-Za-z0-9_]{3,20}$", handle):
            return jsonify({"error": "Handle: 3-20 chars, letters/numbers/_."}), 400
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            return jsonify({"error": "Invalid email format."}), 400
        conn = get_db()
        if exec(conn, "SELECT 1 FROM users WHERE handle=?", (handle,)).fetchone():
            conn.close()
            return jsonify({"error": "Handle taken."}), 400
        if exec(conn, "SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            conn.close()
            return jsonify({"error": "Email already registered."}), 400
        import psycopg
        is_pg = isinstance(conn, psycopg.Connection)
        if is_pg:
            cur = exec(conn,
                "INSERT INTO users (real_name, email, handle, password_hash) VALUES (?,?,?,?) RETURNING id",
                (real_name, email, handle, generate_password_hash(password)),
            )
            conn.commit()
            uid = cur.fetchone()["id"]
        else:
            cur = exec(conn,
                "INSERT INTO users (real_name, email, handle, password_hash) VALUES (?,?,?,?)",
                (real_name, email, handle, generate_password_hash(password)),
            )
            conn.commit()
            uid = cur.lastrowid
        conn.close()
        session["user_id"] = uid
        return jsonify({"ok": True, "handle": handle, "user_id": uid}), 201

    @app.post("/api/login")
    def login():
        p = request.get_json(silent=True) or {}
        # Accept either email or handle as identifier
        identifier = (p.get("identifier") or p.get("handle") or p.get("email") or "").strip().lower()
        password = p.get("password") or ""
        if not identifier or not password:
            return jsonify({"error": "Identifier (email or handle) and password required."}), 400
        conn = get_db()
        # Check if identifier looks like an email
        if "@" in identifier:
            row = exec(conn,
                "SELECT * FROM users WHERE email=?",
                (identifier,),
            ).fetchone()
        else:
            row = exec(conn,
                "SELECT * FROM users WHERE handle=?",
                (identifier,),
            ).fetchone()
        conn.close()
        if not row or not check_password_hash(row["password_hash"], password):
            return jsonify({"error": "Invalid credentials."}), 401
        if row["banned"]:
            return jsonify({"error": "This account has been removed."}), 403
        session["user_id"] = row["id"]
        return jsonify({"ok": True, "handle": row["handle"], "user_id": row["id"]})

    @app.post("/api/logout")
    def logout():
        # Only drop the user identity — an admin session in the same
        # browser must survive logging out of the chat app.
        session.pop("user_id", None)
        return jsonify({"ok": True})

    @app.post("/api/rumors")
    def post_rumor():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        text = (p.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Rumor text is required."}), 400
        raw_tags = p.get("tags") or []
        # sanitize: list of 1-20 char slug-ish names, max 5 tags
        tags = []
        for t in raw_tags:
            name = str(t).strip().lower()[:20]
            if name and name not in tags:
                tags.append(name)
        tags = tags[:5]
        created_at = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        import psycopg
        is_pg = isinstance(conn, psycopg.Connection)
        if is_pg:
            cur = exec(conn,
                "INSERT INTO rumors (user_id, text, created_at) VALUES (?,?,?) "
                "RETURNING id",
                (session["user_id"], text, created_at),
            )
            conn.commit()
            rid = cur.fetchone()["id"]
        else:
            cur = exec(conn,
                "INSERT INTO rumors (user_id, text, created_at) VALUES (?,?,?)",
                (session["user_id"], text, created_at),
            )
            conn.commit()
            rid = cur.lastrowid
        for name in tags:
            tid = _upsert_tag(conn, name)
            exists = exec(conn,
                "SELECT 1 FROM rumor_tags WHERE rumor_id=? AND tag_id=?",
                (rid, tid)).fetchone()
            if not exists:
                exec(conn,
                    "INSERT INTO rumor_tags (rumor_id, tag_id) VALUES (?,?)",
                    (rid, tid))
        conn.commit()
        row = exec(conn,
            "SELECT r.id, r.user_id, r.text, r.created_at, r.bumped_at, u.handle, u.custom_alias, "
            "r.highlighted, r.is_incognito FROM rumors r "
            "JOIN users u ON u.id = r.user_id WHERE r.id = ?", (rid,)
        ).fetchone()
        out = rumor_public(row, conn)
        conn.close()
        return jsonify(out), 201

    @app.get("/api/rumors")
    def list_rumors():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        sort = (request.args.get("sort") or "new").strip().lower()
        tag = (request.args.get("tag") or "").strip().lower()
        filt = (request.args.get("filter") or "").strip().lower()
        conn = get_db()
        base = (("SELECT r.id, r.user_id, r.text, r.created_at, r.bumped_at, u.handle, u.custom_alias, "
                "r.highlighted, r.is_incognito "
                "FROM rumors r JOIN users u ON u.id = r.user_id "
                "WHERE u.banned = 0"))
        params = ()
        if tag:
            base = ("SELECT r.id, r.user_id, r.text, r.created_at, r.bumped_at, u.handle, u.custom_alias, "
                    "r.highlighted, r.is_incognito "
                    "FROM rumors r JOIN users u ON u.id = r.user_id "
                    "JOIN rumor_tags rt ON rt.rumor_id = r.id "
                    "JOIN tags t ON t.id = rt.tag_id "
                    "WHERE u.banned = 0 AND t.name = ?")
            params = (tag,)
        elif filt == "followed" and session.get("user_id"):
            base = ("SELECT r.id, r.user_id, r.text, r.created_at, r.bumped_at, u.handle, u.custom_alias, "
                    "r.highlighted, r.is_incognito "
                    "FROM rumors r JOIN users u ON u.id = r.user_id "
                    "JOIN rumor_tags rt ON rt.rumor_id = r.id "
                    "WHERE u.banned = 0 AND rt.tag_id IN ("
                    "SELECT tag_id FROM tag_follows WHERE user_id = ?)")
            params = (session["user_id"],)
        order_clause = "ORDER BY r.created_at DESC, r.id DESC"
        if sort == "hot":
            # Hot: highlighted posts first, then recently bumped, then newest
            order_clause = ("ORDER BY r.highlighted DESC, "
                            "r.bumped_at DESC NULLS LAST, "
                            "r.created_at DESC, r.id DESC")
        elif sort == "rising":
            # Rising: newest first (no reactions data to compute a rising score)
            order_clause = "ORDER BY r.created_at DESC, r.id DESC"
        rows = exec(conn, base + " " + order_clause, params).fetchall()
        out = [rumor_public(r, conn) for r in rows]
        conn.close()
        return jsonify({"rumors": out})

    @app.get("/api/rumors/<int:rid>/teaser")
    def rumor_teaser(rid):
        # Information-gap trigger: hide the text, show a curiosity teaser.
        conn = get_db()
        row = exec(conn,
            "SELECT r.id, r.text, r.created_at, r.bumped_at, u.handle, u.custom_alias, "
            "r.highlighted, r.is_incognito FROM rumors r "
            "JOIN users u ON u.id = r.user_id "
            "WHERE r.id=? AND u.banned=0", (rid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Rumor not found."}), 404
        teaser = _make_teaser(row["text"])
        try:
            custom_alias = row["custom_alias"] if row["custom_alias"] else None
        except (KeyError, IndexError, TypeError):
            custom_alias = None
        try:
            is_inc = int(row["is_incognito"])
        except (KeyError, IndexError, TypeError, ValueError):
            is_inc = 0
        handle = custom_alias if custom_alias else row["handle"]
        if is_inc:
            handle = "👻 Ghost"
        data = {
            "id": row["id"],
            "handle": handle,
            "teaser": teaser,
            "created_at": row["created_at"],
        }
        conn.close()
        return jsonify(data)


    # --- Feature 3: Posting streak (Self reward + loss aversion) ---
    @app.get("/api/me")
    def me():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        from datetime import datetime, timezone, timedelta
        conn = get_db()
        uid = session["user_id"]
        row = exec(conn, "SELECT handle, selected_badge FROM users WHERE id=?",
                   (uid,)).fetchone()
        if row is None:
            session.clear()
            conn.close()
            return jsonify({"error": "User not found."}), 404
        streak, at_risk, run = _compute_streak(conn, uid)
        # Streak Shield: a full 2-day miss would zero the streak; if the user
        # owns a shield, consume one and restore the streak as of their last
        # active day (today becomes the final grace day).
        shield = exec(conn, "SELECT streak_shield FROM users WHERE id=?",
                      (uid,)).fetchone()
        shield_n = shield["streak_shield"] if shield and shield["streak_shield"] else 0
        if streak == 0 and shield_n > 0:
            exec(conn, "UPDATE users SET streak_shield = streak_shield - 1 WHERE id=?",
                 (uid,))
            conn.commit()
            streak, at_risk = run, True
            shield_n -= 1
        points = _compute_points(conn, uid)
        badges = _compute_badges(conn, uid)
        rank = _user_rank(conn, uid)
        # Check if user has active featured badge
        now_iso = datetime.now(timezone.utc).isoformat()
        feat = exec(conn,
            "SELECT id FROM purchases WHERE user_id=? AND kind='featured' "
            "AND (expires_at IS NULL OR expires_at > ?)", (uid, now_iso)
        ).fetchone()
        boost = exec(conn, "SELECT boost_until FROM users WHERE id=?",
                     (uid,)).fetchone()
        boost_active = bool(boost and boost["boost_until"]
                            and boost["boost_until"] > now_iso)
        ncol = exec(conn, "SELECT name_color, name_color_until FROM users WHERE id=?",
                    (uid,)).fetchone()
        name_color = None
        if ncol and ncol["name_color"] and ncol["name_color_until"] \
                and ncol["name_color_until"] > now_iso:
            name_color = ncol["name_color"]
        recent_bumped = exec(conn,
            "SELECT id, text, created_at, bumped_at FROM rumors "
            "WHERE user_id=? AND bumped_at IS NOT NULL AND bumped_at >= ? "
            "ORDER BY bumped_at DESC",
            (uid, (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
        ).fetchall()
        recent_bumped_out = [{
            "id": r["id"],
            "text": r["text"],
            "created_at": r["created_at"],
            "bumped_at": r["bumped_at"],
            "preview": r["text"][:80],
        } for r in recent_bumped]
        conn.close()
        return jsonify({"handle": row["handle"],
                        "user_id": uid,
                        "selected_badge": row["selected_badge"],
                        "streak": streak,
                        "streak_at_risk_today": at_risk,
                        "points": points, "badges": badges, "rank": rank,
                        "featured": bool(feat),
                        "boost_active": boost_active,
                        "streak_shield": shield_n,
                        "name_color": name_color,
                        "recent_bumped": recent_bumped_out})

    # --- Reward system: challenges, leaderboard (anonymized) ---
    @app.get("/api/challenges")
    def list_challenges():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        conn = get_db()
        uid = session["user_id"]
        claimed = set()
        for r in exec(conn,
                "SELECT challenge_key, week FROM challenge_claims WHERE user_id=?",
                (uid,)).fetchall():
            claimed.add((r["challenge_key"], r["week"]))
        out = []
        for key, label, goal, reward, kind in CHALLENGE_DEFS:
            scope = _challenge_scope(kind)
            progress = _challenge_progress(conn, uid, kind)
            out.append({
                "key": key, "label": label, "goal": goal, "reward": reward,
                "progress": min(progress, goal),
                "completed": progress >= goal,
                "claimed": (key, scope) in claimed,
            })
        conn.close()
        return jsonify({"week": _current_week(), "challenges": out})

    @app.post("/api/challenges/<key>/claim")
    def claim_challenge(key):
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        cdef = next((c for c in CHALLENGE_DEFS if c[0] == key), None)
        if not cdef:
            return jsonify({"error": "Unknown challenge."}), 404
        _k, _label, goal, _reward, kind = cdef
        conn = get_db()
        uid = session["user_id"]
        scope = _challenge_scope(kind)
        if _challenge_progress(conn, uid, kind) < goal:
            conn.close()
            return jsonify({"error": "Challenge not complete yet."}), 400
        already = exec(conn,
            "SELECT 1 FROM challenge_claims WHERE user_id=? AND challenge_key=? AND week=?",
            (uid, key, scope)).fetchone()
        if already:
            conn.close()
            return jsonify({"error": "Already claimed this week."}), 400
        exec(conn,
            "INSERT INTO challenge_claims (user_id, challenge_key, week, claimed_at) "
            "VALUES (?,?,?,?)",
            (uid, key, scope, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        points = _compute_points(conn, uid)
        conn.close()
        return jsonify({"ok": True, "claimed": key, "points": points})

    @app.get("/api/leaderboard")
    def leaderboard():
        conn = get_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        featured_users = set()
        for r in exec(conn,
            "SELECT DISTINCT user_id FROM purchases WHERE kind='featured' "
            "AND (expires_at IS NULL OR expires_at > ?)", (now_iso,)
        ).fetchall():
            featured_users.add(r[0] if not hasattr(r, "keys") else r["user_id"])
        # Compute points fresh in one pass (the cached users.points column is
        # never committed, so it would show stale zeros). Aggregate each
        # activity table with GROUP BY, then combine in Python.
        score = {}
        for r in exec(conn,
            "SELECT id, points_awarded, points_spent FROM users WHERE banned=0"
        ).fetchall():
            score[r["id"]] = {
                "earned": 0,
                "awarded": r["points_awarded"] or 0,
                "spent": r["points_spent"] or 0,
            }
        def add(rows, mul):
            for r in rows:
                uid = r["user_id"]
                if uid in score:
                    score[uid]["earned"] += r["n"] * mul
        add(exec(conn,
            "SELECT user_id, COUNT(*) AS n FROM room_messages GROUP BY user_id"
        ).fetchall(), PTS_POST)
        add(exec(conn,
            "SELECT user_id, COUNT(*) AS n FROM rumors GROUP BY user_id"
        ).fetchall(), PTS_POST)
        add(exec(conn,
            "SELECT user_id, COUNT(*) AS n FROM reactions GROUP BY user_id"
        ).fetchall(), PTS_REACT_GIVEN)
        add(exec(conn,
            "SELECT r.user_id, COUNT(*) AS n FROM reactions rx "
            "JOIN rumors r ON r.id = rx.rumor_id GROUP BY r.user_id"
        ).fetchall(), PTS_REACT_RECEIVED)
        add(exec(conn,
            "SELECT r.user_id, COUNT(*) AS n FROM me_too m "
            "JOIN rumors r ON r.id = m.rumor_id GROUP BY r.user_id"
        ).fetchall(), PTS_METOO_RECEIVED)
        reward_by_key = {c[0]: c[3] for c in CHALLENGE_DEFS}
        for r in exec(conn,
            "SELECT user_id, challenge_key FROM challenge_claims"
        ).fetchall():
            uid = r["user_id"]
            if uid in score:
                score[uid]["earned"] += reward_by_key.get(r["challenge_key"], 0)
        conn.close()
        scored = sorted(
            ((uid, max(v["earned"] + v["awarded"] - v["spent"], 0))
             for uid, v in score.items()),
            key=lambda t: (-t[1], t[0]))
        # Anonymized: alias only (Player #N), never the handle/email/real_name.
        out = []
        viewer_uid = session.get("user_id")
        viewer_seen = False
        for i, (uid, pts) in enumerate(scored[:10], start=1):
            entry = {"rank": i, "alias": f"Player #{i}", "points": pts}
            if uid in featured_users:
                entry["featured"] = True
            if viewer_uid and uid == viewer_uid:
                entry["is_me"] = True
                viewer_seen = True
            out.append(entry)
        # Always include the viewer's own rank, even outside the top 10.
        if viewer_uid and not viewer_seen:
            for i, (uid, pts) in enumerate(scored, start=1):
                if uid == viewer_uid:
                    entry = {"rank": i, "alias": f"Player #{i}", "points": pts, "is_me": True}
                    if uid in featured_users:
                        entry["featured"] = True
                    out.append(entry)
                    break
        return jsonify({"leaderboard": out})

    @app.post("/api/badge/select")
    def badge_select():
        """Set the user's active badge flair."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        key = (p.get("key") or "").strip()
        uid = session["user_id"]
        conn = get_db()
        badges = _compute_badges(conn, uid)
        owned_keys = [b["key"] for b in badges]
        if key and key not in owned_keys:
            conn.close()
            return jsonify({"error": "You don't own this badge."}), 400
        exec(conn, "UPDATE users SET selected_badge=? WHERE id=?", (key or None, uid))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "selected_badge": key or None})

    # --- Shop: spend points on cosmetic/perk features ---
    @app.get("/api/shop")
    def list_shop():
        """Return available shop items with prices + the user's boost state."""
        items = {
            k: {"label": v[0], "desc": v[1], "price": v[2], "kind": k}
            for k, v in SHOP_ITEMS.items()
        }
        me = None
        if session.get("user_id"):
            from datetime import datetime as _dt, timezone as _tz
            conn = get_db()
            uid = session["user_id"]
            now_iso = _dt.now(_tz.utc).isoformat()
            boost = exec(conn, "SELECT boost_until FROM users WHERE id=?",
                         (uid,)).fetchone()
            shield = exec(conn, "SELECT streak_shield FROM users WHERE id=?",
                          (uid,)).fetchone()
            ncol = exec(conn,
                        "SELECT name_color, name_color_until FROM users WHERE id=?",
                        (uid,)).fetchone()
            conn.close()
            name_color = None
            if ncol and ncol["name_color"] and ncol["name_color_until"] \
                    and ncol["name_color_until"] > now_iso:
                name_color = ncol["name_color"]
            me = {
                "boost_active": bool(boost and boost["boost_until"]
                                     and boost["boost_until"] > now_iso),
                "streak_shield": shield["streak_shield"] if shield else 0,
                "name_color": name_color,
                "palette": NAME_COLORS,
            }
        return jsonify({"items": items, "me": me})

    @app.get("/api/me/whispers")
    def my_whispers():
        """Return the current user's own whispers (for targeting purchases)."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        conn = get_db()
        rows = exec(conn,
            "SELECT r.id, substr(r.text, 1, 80) AS preview, r.created_at, r.bumped_at, "
            "r.highlighted, r.is_incognito, u.custom_alias "
            "FROM rumors r JOIN users u ON u.id = r.user_id "
            "WHERE r.user_id=? ORDER BY r.id DESC", (session["user_id"],)
        ).fetchall()
        conn.close()
        return jsonify({"whispers": [dict(r) for r in rows]})

    @app.post("/api/shop/buy")
    def shop_buy():
        """Purchase a shop item. Deducts points from the user's balance."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        kind = (p.get("kind") or "").strip()
        rumor_id = p.get("rumor_id")
        alias = (p.get("alias") or "").strip()

        if kind not in SHOP_ITEMS:
            return jsonify({"error": "Unknown item."}), 400

        price = SHOP_ITEMS[kind][2]
        uid = session["user_id"]
        conn = get_db()

        # Check affordability
        points = _compute_points(conn, uid)
        if points < price:
            conn.close()
            return jsonify({"error": "Not enough points.",
                            "points": points, "price": price}), 400

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).isoformat()

        # Validate and apply
        if kind == "alias":
            if not alias:
                conn.close()
                return jsonify({"error": "Alias text required."}), 400
            if len(alias) > 30:
                conn.close()
                return jsonify({"error": "Alias too long (max 30 chars)."}), 400
            exec(conn, "UPDATE users SET custom_alias=? WHERE id=?",
                 (alias, uid))
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, meta, created_at) "
                 "VALUES (?, ?, ?, ?)",
                 (uid, kind, alias, now))

        elif kind == "name_color":
            color = (p.get("color") or "").strip().lower()
            match = next((c for c in NAME_COLORS if c.lower() == color), None)
            if not match:
                conn.close()
                return jsonify({"error": "Pick a color from the palette."}), 400
            expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            exec(conn,
                 "UPDATE users SET name_color=?, name_color_until=? WHERE id=?",
                 (match, expires, uid))
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, meta, created_at, expires_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (uid, kind, match, now, expires))

        elif kind in MESSAGE_ITEMS:
            # highlight / ghost / pin target one of your own room messages
            message_id = p.get("message_id") or p.get("rumor_id")
            if not message_id:
                conn.close()
                return jsonify({"error": "message_id required."}), 400
            own = exec(conn,
                "SELECT id FROM room_messages WHERE id=? AND user_id=?",
                (message_id, uid)).fetchone()
            if not own:
                conn.close()
                return jsonify({"error": "Message not found or not yours."}), 404
            if kind == "highlight":
                exec(conn,
                     "UPDATE room_messages SET highlighted=1, is_incognito=0 "
                     "WHERE id=?", (message_id,))
            elif kind == "ghost":
                exec(conn,
                     "UPDATE room_messages SET is_incognito=1, highlighted=0 "
                     "WHERE id=?", (message_id,))
            elif kind == "pin":
                # One active pin per user: retire old pins, pin for 24h
                exec(conn,
                     "UPDATE room_messages SET pinned_until=NULL WHERE user_id=? "
                     "AND pinned_until IS NOT NULL AND pinned_until > ?",
                     (uid, now))
                pin_until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
                exec(conn, "UPDATE room_messages SET pinned_until=? WHERE id=?",
                     (pin_until, message_id))
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, rumor_id, created_at) "
                 "VALUES (?, ?, ?, ?)",
                 (uid, kind, message_id, now))

        elif kind == "streak_shield":
            exec(conn,
                 "UPDATE users SET streak_shield = COALESCE(streak_shield,0) + 1 "
                 "WHERE id=?", (uid,))
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, created_at) "
                 "VALUES (?, ?, ?)", (uid, kind, now))

        elif kind == "boost":
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            exec(conn, "UPDATE users SET boost_until=? WHERE id=?", (expires, uid))
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, meta, created_at, expires_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (uid, kind, expires, now, expires))

        elif kind == "mystery_box":
            import random as _rnd
            roll = _rnd.random()
            prize_label, prize_pts, prize_kind = "30 pts", 30, "pts"
            cum = 0.0
            for weight, pts, label in MYSTERY_PRIZES:
                cum += weight
                if roll <= cum:
                    prize_pts, prize_label = pts, label
                    prize_kind = "pts" if pts else "badge"
                    break
            if prize_kind == "pts":
                exec(conn,
                     "UPDATE users SET points_awarded = COALESCE(points_awarded,0) + ? "
                     "WHERE id=?", (prize_pts, uid))
            else:
                # Free random flair badge; if they own all, fall back to 150 pts
                owned = {r["meta"] for r in exec(conn,
                    "SELECT meta FROM purchases WHERE user_id=? AND kind='badge'",
                    (uid,)).fetchall()}
                avail = [k for k in SHOP_ITEMS
                         if k.startswith("badge_") and k not in owned]
                if avail:
                    prize_kind = "badge"
                    pick = _rnd.choice(avail)
                    prize_label = SHOP_ITEMS[pick][0]
                    exec(conn,
                         "INSERT INTO purchases (user_id, kind, meta, created_at) "
                         "VALUES (?, ?, ?, ?)",
                         (uid, "badge", pick, now))
                else:
                    prize_pts = 150
                    prize_label = "150 pts"
                    exec(conn,
                         "UPDATE users SET points_awarded = COALESCE(points_awarded,0) + ? "
                         "WHERE id=?", (prize_pts, uid))
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, meta, created_at) "
                 "VALUES (?, ?, ?, ?)",
                 (uid, kind, f"won {prize_label}", now))
            prize_out = f"You won {prize_label}!"

        elif kind == "featured":
            expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, created_at, expires_at) "
                 "VALUES (?, ?, ?, ?)",
                 (uid, kind, now, expires))

        elif kind.startswith("badge_"):
            # Purchasable flair badge — check not already owned
            existing = exec(conn,
                "SELECT 1 FROM purchases WHERE user_id=? AND kind='badge' AND meta=?",
                (uid, kind)).fetchone()
            if existing:
                conn.close()
                return jsonify({"error": "You already own this badge."}), 400
            exec(conn,
                 "INSERT INTO purchases (user_id, kind, meta, created_at) "
                 "VALUES (?, ?, ?, ?)",
                 (uid, "badge", kind, now))

        # Deduct points
        exec(conn,
             "UPDATE users SET points_spent = points_spent + ? WHERE id=?",
             (price, uid))
        conn.commit()
        new_points = _compute_points(conn, uid)
        conn.close()
        return jsonify({"ok": True, "points": new_points,
                        "prize": prize_out if kind == "mystery_box" else None})


    # --- Feature 5: Tags + follow-a-tag (Investment / internal trigger) ---
    @app.get("/api/tags")
    def list_tags():
        conn = get_db()
        rows = exec(conn,
            "SELECT t.name, COUNT(rt.rumor_id) AS count FROM tags t "
            "LEFT JOIN rumor_tags rt ON rt.tag_id = t.id "
            "GROUP BY t.id ORDER BY count DESC, t.name"
        ).fetchall()
        conn.close()
        return jsonify({"tags": [dict(r) for r in rows]})

    @app.post("/api/tags/<name>/follow")
    def follow_tag(name):
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        name = name.strip().lower()[:20]
        if not name:
            return jsonify({"error": "Tag name required."}), 400
        conn = get_db()
        tid = _upsert_tag(conn, name)
        exists = exec(conn,
            "SELECT 1 FROM tag_follows WHERE user_id=? AND tag_id=?",
            (session["user_id"], tid)).fetchone()
        if not exists:
            exec(conn,
                "INSERT INTO tag_follows (user_id, tag_id) VALUES (?,?)",
                (session["user_id"], tid))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "following": name})

    @app.delete("/api/tags/<name>/follow")
    def unfollow_tag(name):
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        name = name.strip().lower()[:20]
        conn = get_db()
        row = exec(conn, "SELECT id FROM tags WHERE name=?",
                   (name,)).fetchone()
        if row:
            exec(conn,
                "DELETE FROM tag_follows WHERE user_id=? AND tag_id=?",
                (session["user_id"], row["id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "unfollowed": name})

    @app.get("/api/me/tags")
    def my_tags():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        conn = get_db()
        rows = exec(conn,
            "SELECT t.name FROM tag_follows tf JOIN tags t ON t.id=tf.tag_id "
            "WHERE tf.user_id=?", (session["user_id"],)).fetchall()
        conn.close()
        return jsonify({"followed_tags": [r["name"] for r in rows]})


    @app.post("/api/admin/login")
    def admin_login():
        # Admin access is password-only (the owner's secret). No email gate.
        p = request.get_json(silent=True) or {}
        if p.get("password") != app.config["ADMIN_PASSWORD"]:
            return jsonify({"error": "Unauthorized."}), 401
        # Admin and user identities live in the SAME cookie and are keyed
        # separately ("admin" vs "user_id") — the owner uses both panels in
        # one browser. Never clear the whole session here, or admin login
        # would log the user out of the chat app.
        session["admin"] = True
        return jsonify({"ok": True})

    @app.get("/api/admin/me")
    def admin_me():
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        return jsonify({"ok": True})

    @app.post("/api/admin/logout")
    def admin_logout():
        session.pop("admin", None)
        return jsonify({"ok": True})

    @app.post("/api/forgot-password")
    def forgot_password():
        # Privacy-safe: never confirms whether a handle exists. The admin is
        # notified (log) so they can reset it manually via the admin panel.
        p = request.get_json(silent=True) or {}
        handle = (p.get("handle") or "").strip()
        if not handle:
            return jsonify({"error": "Handle required."}), 400
        conn = get_db()
        row = exec(conn, "SELECT id, handle FROM users WHERE handle=?",
                   (handle,)).fetchone()
        conn.close()
        if row:
            app.logger.info(
                "Campus Whispers: password-reset requested for user %s",
                row["handle"])
        # Always return the same neutral message (no account enumeration).
        return jsonify({
            "ok": True,
            "message": "If that handle is registered, the admin has been "
                       "notified and will reset your password."
        }), 200

    @app.post("/api/admin/users/<int:uid>/reset-password")
    def admin_reset_password(uid):
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        p = request.get_json(silent=True) or {}
        new_pw = p.get("new_password") or ""
        if len(new_pw) < 4:
            return jsonify({"error": "Password too short (min 4)."}), 400
        conn = get_db()
        row = exec(conn, "SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "User not found."}), 404
        exec(conn, "UPDATE users SET password_hash=? WHERE id=?",
             (generate_password_hash(new_pw), uid))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "reset": uid})


    @app.post("/api/admin/digest/send")
    def admin_digest_send():
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        p = request.get_json(silent=True) or {}
        try:
            window_hours = int(p.get("window_hours") or 24)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid window_hours."}), 400
        window_hours = max(1, min(window_hours, 168))
        conn = get_db()
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=window_hours)).isoformat()
        rows = exec(conn,
            "SELECT r.id, r.text, r.created_at, u.handle "
            "FROM rumors r JOIN users u ON u.id = r.user_id "
            "WHERE u.banned = 0 AND r.created_at >= ? "
            "ORDER BY r.id DESC",
            (cutoff,)).fetchall()
        # Get emails of all non-banned users for digest delivery
        user_emails = exec(conn,
            "SELECT email FROM users WHERE banned=0").fetchall()
        conn.close()
        if not rows:
            return jsonify({"ok": True, "sent": 0, "note": "no recent rumors"})
        body = _render_digest(rows)
        # Extract email addresses
        to_addrs = [u["email"] for u in user_emails]
        # Allow test injection of a fake sender via app.config["MAIL_SENDER"]
        mailer = app.config.get("MAIL_SENDER")
        if mailer:
            for to in to_addrs:
                mailer(to, "Campus Whispers — today's top whispers", body)
            sent = len(to_addrs)
        else:
            sent = _send_digest_smtp(app, to_addrs, body)
        return jsonify({"ok": True, "sent": sent, "rumors": len(rows)})


    @app.get("/api/admin/rumors")
    def admin_rumors():
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        rows = exec(conn,
            "SELECT r.id, r.text, r.created_at, u.handle, u.real_name "
            "FROM rumors r "
            "JOIN users u ON u.id = r.user_id ORDER BY r.id DESC"
        ).fetchall()
        conn.close()
        return jsonify({"rumors": [rumor_admin(r) for r in rows]})

    @app.delete("/api/admin/rumors/<int:rid>")
    def admin_delete_rumor(rid):
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        exec(conn, "DELETE FROM rumors WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "deleted": rid})

    @app.post("/api/admin/rumors/<int:rid>/feature")
    def admin_feature_rumor(rid):
        # Variable-reward surprise: admin (or a future random job) marks a
        # whisper 'featured' — an unpredictable bonus that drives re-checking.
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        if not exec(conn, "SELECT 1 FROM rumors WHERE id=?", (rid,)).fetchone():
            conn.close()
            return jsonify({"error": "Rumor not found."}), 404
        exec(conn, "UPDATE rumors SET featured=1 WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "featured": rid})

    @app.get("/api/admin/users")
    def admin_users():
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        rows = exec(conn, 
            "SELECT id, real_name, email, handle, banned FROM users ORDER BY id"
        ).fetchall()
        users = []
        for r in rows:
            u = dict(r)
            u["points"] = _compute_points(conn, u["id"])
            users.append(u)
        conn.close()
        return jsonify({"users": users})

    @app.post("/api/admin/users/<int:uid>/grant-points")
    def admin_grant_points(uid):
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        payload = request.get_json(silent=True) or {}
        try:
            amount = int(payload.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid amount."}), 400
        if amount <= 0:
            return jsonify({"error": "Amount must be positive."}), 400
        if amount > 10000:
            return jsonify({"error": "Amount too large."}), 400
        conn = get_db()
        user = exec(conn, "SELECT id, handle, banned FROM users WHERE id=?", (uid,)).fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "User not found."}), 404
        exec(conn,
             "UPDATE users SET points_awarded = COALESCE(points_awarded,0) + ? WHERE id=?",
             (amount, uid))
        conn.commit()
        new_points = _compute_points(conn, uid)
        conn.close()
        return jsonify({"ok": True, "uid": uid, "points": new_points, "awarded": amount})

    @app.delete("/api/admin/users/<int:uid>")
    def admin_ban_user(uid):
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        exec(conn, "UPDATE users SET banned = 1 WHERE id=?", (uid,))
        exec(conn, "DELETE FROM rumors WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "banned": uid})

    # TEMP: pre-launch cleanup endpoint — REMOVE after use
    @app.post("/api/admin/_reset")
    def admin_reset():
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        payload = request.get_json(silent=True) or {}
        dry = bool(payload.get("dry_run"))
        conn = get_db()
        out = {}
        if dry:
            out["users"] = [dict(r) for r in exec(conn,
                "SELECT id, handle, real_name, email FROM users ORDER BY id").fetchall()]
        for t in ["reactions", "me_too", "comments", "rumor_tags", "tag_follows",
                  "conversation_participants", "messages", "conversations",
                  "room_messages", "rumors", "tags", "purchases",
                  "challenge_claims", "push_subs"]:
            try:
                n = exec(conn, f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                if not dry:
                    exec(conn, f"DELETE FROM {t}")
                out[t] = n
            except Exception as e:
                out[t] = f"ERR {e}"
                if not dry:
                    conn.rollback()
                    conn.close()
                    return jsonify({"error": "wipe aborted", "table": t, "detail": str(e)}), 500
        n_users = exec(conn, "SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        out["users_count"] = n_users
        if not dry and payload.get("wipe_users"):
            exec(conn, "DELETE FROM users")
            out["users_deleted"] = n_users
        conn.commit()
        conn.close()
        return jsonify(out)

    @app.get("/api/admin/chat")
    def admin_chat():
        """Live group-chat moderation feed (newest first)."""
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        rows = exec(conn,
            "SELECT rm.id, rm.user_id, rm.text, rm.created_at, u.handle, "
            "u.real_name, u.banned "
            "FROM room_messages rm JOIN users u ON u.id = rm.user_id "
            "ORDER BY rm.id DESC LIMIT 200",
            ()).fetchall()
        msgs = [{
            "id": r["id"],
            "sender_id": r["user_id"],
            "handle": r["handle"],
            "real_name": r["real_name"],
            "banned": bool(r["banned"]),
            "text": r["text"],
            "created_at": r["created_at"],
        } for r in rows]
        conn.close()
        return jsonify({"messages": msgs})

    @app.delete("/api/admin/chat/<int:mid>")
    def admin_delete_chat_message(mid):
        if not session.get("admin"):
            return jsonify({"error": "Unauthorized."}), 401
        conn = get_db()
        cur = exec(conn, "DELETE FROM room_messages WHERE id=?", (mid,))
        conn.commit()
        deleted = cur.rowcount if hasattr(cur, "rowcount") else 1
        conn.close()
        if not deleted:
            return jsonify({"error": "Message not found."}), 404
        return jsonify({"ok": True, "deleted": mid})

    # === Admin: (questions feature removed) ===

    @app.get("/")
    def index():
        return serve_page("index.html")

    @app.get("/admin")
    def admin_page():
        return serve_page("admin.html")

    @app.get("/sw.js")
    def sw_js():
        # Root-scoped so the service worker controls the whole app
        resp = app.make_response(serve_page("sw.js"))
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/manifest.json")
    def manifest_json():
        resp = app.make_response(serve_page("manifest.json"))
        resp.headers["Content-Type"] = "application/manifest+json"
        return resp

    @app.after_request
    def security_headers(resp):
        # Helmet equivalent for Flask (spec: security headers)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "worker-src 'self'; "
            "manifest-src 'self'"
        )
        return resp

    # === REST endpoints for conversations ===

    @app.get("/api/users/search")
    def search_users():
        """Search users by handle for starting a conversation."""
        q = (request.args.get("q") or "").strip().lower()
        if len(q) < 1:
            return jsonify({"users": []})
        conn = get_db()
        rows = exec(conn,
            "SELECT id, handle FROM users WHERE banned=0 AND handle LIKE ? "
            "ORDER BY handle LIMIT 10",
            (f"%{q}%",)).fetchall()
        conn.close()
        return jsonify({"users": [{"id": r["id"], "handle": r["handle"]} for r in rows]})

    @app.post("/api/conversations")
    def create_conversation():
        """Start a new DM with another user."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        target_id = p.get("user_id")
        if not target_id or target_id == session["user_id"]:
            return jsonify({"error": "Invalid target user."}), 400
        conn = get_db()
        # Check target exists
        target = exec(conn, "SELECT id FROM users WHERE id=? AND banned=0",
                      (target_id,)).fetchone()
        if not target:
            conn.close()
            return jsonify({"error": "User not found."}), 404
        uid = session["user_id"]
        import psycopg
        is_pg = isinstance(conn, psycopg.Connection)
        # Check existing conversation between these two users
        existing = exec(conn,
            "SELECT cp1.conversation_id FROM conversation_participants cp1 "
            "JOIN conversation_participants cp2 "
            "ON cp1.conversation_id = cp2.conversation_id "
            "WHERE cp1.user_id=? AND cp2.user_id=? AND cp1.user_id < cp2.user_id",
            (uid, target_id)).fetchone()
        if existing:
            conn.close()
            return jsonify({"conversation_id": existing["conversation_id"]})
        # Create new conversation
        now = datetime.now(timezone.utc).isoformat()
        if is_pg:
            cur = exec(conn,
                "INSERT INTO conversations (created_at) VALUES (?) RETURNING id",
                (now,))
            conn.commit()
            conv_id = cur.fetchone()["id"]
        else:
            cur = exec(conn, "INSERT INTO conversations (created_at) VALUES (?)", (now,))
            conn.commit()
            conv_id = cur.lastrowid
        # Add participants
        exec(conn,
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?,?)",
            (conv_id, uid))
        exec(conn,
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?,?)",
            (conv_id, target_id))
        conn.commit()
        conn.close()
        return jsonify({"conversation_id": conv_id}), 201

    @app.get("/api/conversations")
    def list_conversations():
        """List the current user's conversations with last message preview."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        uid = session["user_id"]
        conn = get_db()
        rows = exec(conn,
            "SELECT c.id AS conv_id, "
            "  (SELECT handle FROM users WHERE id = "
            "    (SELECT cp2.user_id FROM conversation_participants cp2 "
            "     WHERE cp2.conversation_id = c.id AND cp2.user_id != ?)"
            "  ) AS other_handle, "
            "  (SELECT text FROM messages WHERE conversation_id = c.id "
            "   ORDER BY id DESC LIMIT 1) AS last_message, "
            "  (SELECT created_at FROM messages WHERE conversation_id = c.id "
            "   ORDER BY id DESC LIMIT 1) AS last_at "
            "FROM conversations c "
            "WHERE c.id IN ("
            "  SELECT conversation_id FROM conversation_participants WHERE user_id=?"
            ") ORDER BY last_at DESC NULLS LAST, c.id DESC",
            (uid, uid)).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append({
                "id": r["conv_id"],
                "other_handle": r["other_handle"],
                "last_message": r["last_message"],
                "last_at": r["last_at"],
            })
        return jsonify({"conversations": out})

    @app.get("/api/conversations/<int:conv_id>/messages")
    def get_messages(conv_id):
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        uid = session["user_id"]
        conn = get_db()
        # Verify participant
        row = exec(conn,
            "SELECT 1 FROM conversation_participants "
            "WHERE conversation_id=? AND user_id=?",
            (conv_id, uid)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Not a participant."}), 403
        before = request.args.get("before")
        limit = min(int(request.args.get("limit") or 50), 100)
        if before:
            rows = exec(conn,
                "SELECT m.id, m.sender_id, u.handle AS sender_handle, "
                "m.text, m.created_at FROM messages m "
                "JOIN users u ON u.id = m.sender_id "
                "WHERE m.conversation_id=? AND m.id < ? "
                "ORDER BY m.id DESC LIMIT ?",
                (conv_id, before, limit)).fetchall()
        else:
            rows = exec(conn,
                "SELECT m.id, m.sender_id, u.handle AS sender_handle, "
                "m.text, m.created_at FROM messages m "
                "JOIN users u ON u.id = m.sender_id "
                "WHERE m.conversation_id=? "
                "ORDER BY m.id DESC LIMIT ?",
                (conv_id, limit)).fetchall()
        conn.close()
        msgs = [{
            "id": r["id"], "sender_id": r["sender_id"],
            "sender_handle": r["sender_handle"],
            "text": r["text"], "created_at": r["created_at"],
        } for r in reversed(rows)]
        return jsonify({"messages": msgs})

    @app.post("/api/conversations/<int:conv_id>/messages")
    def send_message(conv_id):
        """Send a message in a conversation."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        uid = session["user_id"]
        p = request.get_json(silent=True) or {}
        text = (p.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Message text required."}), 400
        conn = get_db()
        import psycopg
        is_pg = isinstance(conn, psycopg.Connection)
        # Verify user is a participant
        row = exec(conn,
            "SELECT 1 FROM conversation_participants "
            "WHERE conversation_id=? AND user_id=?",
            (conv_id, uid)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Not a participant."}), 403
        created_at = datetime.now(timezone.utc).isoformat()
        if is_pg:
            cur = exec(conn,
                "INSERT INTO messages (conversation_id, sender_id, text, created_at) "
                "VALUES (?,?,?,?) RETURNING id",
                (conv_id, uid, text, created_at))
            conn.commit()
            mid = cur.fetchone()["id"]
        else:
            cur = exec(conn,
                "INSERT INTO messages (conversation_id, sender_id, text, created_at) "
                "VALUES (?,?,?,?)",
                (conv_id, uid, text, created_at))
            conn.commit()
            mid = cur.lastrowid
        # Get sender handle
        handle_row = exec(conn, "SELECT handle FROM users WHERE id=?", (uid,)).fetchone()
        handle = handle_row["handle"] if handle_row else "unknown"
        conn.close()
        return jsonify({
            "id": mid, "conversation_id": conv_id,
            "sender_id": uid, "sender_handle": handle,
            "text": text, "created_at": created_at,
        }), 201

    # === Group room (the feed is a shared group chat) ===

    @app.get("/api/feed")
    def list_room_messages():
        """Last 200 room messages (oldest-first) + active pins, with sender info."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        conn = get_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        # Active pins (24h) always surface on top, newest pin first
        pins = exec(conn,
            "SELECT rm.id, rm.user_id, rm.text, rm.created_at, rm.pinned_until, "
            "rm.highlighted, rm.is_incognito, u.handle, u.name_color "
            "FROM room_messages rm JOIN users u ON u.id = rm.user_id "
            "WHERE rm.pinned_until IS NOT NULL AND rm.pinned_until > ? "
            "ORDER BY rm.pinned_until DESC",
            (now_iso,)).fetchall()
        # Normal flow: exclude pinned messages so nothing renders twice
        rows = exec(conn,
            "SELECT rm.id, rm.user_id, rm.text, rm.created_at, rm.pinned_until, "
            "rm.highlighted, rm.is_incognito, u.handle, u.name_color "
            "FROM room_messages rm JOIN users u ON u.id = rm.user_id "
            "WHERE rm.pinned_until IS NULL OR rm.pinned_until <= ? "
            "ORDER BY rm.id DESC LIMIT 200",
            (now_iso,)).fetchall()

        def msg(r):
            return {
                "id": r["id"],
                "sender_id": r["user_id"],
                "sender_handle": r["handle"],
                "text": r["text"],
                "created_at": r["created_at"],
                "highlighted": bool(r["highlighted"]),
                "is_incognito": bool(r["is_incognito"]),
                "pinned_until": r["pinned_until"],
                "name_color": r["name_color"],
            }

        pinned = [msg(r) for r in pins]
        msgs = [msg(r) for r in reversed(rows)]
        conn.close()
        return jsonify({"messages": msgs, "pinned": pinned})

    @app.get("/api/feed/mine")
    def my_room_messages():
        """The current user's own recent room messages (for shop targeting)."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        conn = get_db()
        uid = session["user_id"]
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = exec(conn,
            "SELECT id, text, created_at, highlighted, is_incognito, pinned_until "
            "FROM room_messages WHERE user_id=? "
            "ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "preview": (r["text"] or "")[:80],
                "created_at": r["created_at"],
                "highlighted": bool(r["highlighted"]),
                "is_incognito": bool(r["is_incognito"]),
                "pinned": bool(r["pinned_until"] and r["pinned_until"] > now_iso),
            })
        return jsonify({"messages": out})

    @app.post("/api/feed")
    def post_room_message():
        """Post a message to the group room; awards points like a post."""
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        text = (p.get("text") or "").strip()[:1000]
        if not text:
            return jsonify({"error": "Message text is required."}), 400
        uid = session["user_id"]
        created_at = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        import psycopg
        if isinstance(conn, psycopg.Connection):
            cur = exec(conn,
                "INSERT INTO room_messages (user_id, text, created_at) "
                "VALUES (?,?,?) RETURNING id",
                (uid, text, created_at))
            conn.commit()
            mid = cur.fetchone()["id"]
        else:
            cur = exec(conn,
                "INSERT INTO room_messages (user_id, text, created_at) "
                "VALUES (?,?,?)",
                (uid, text, created_at))
            conn.commit()
            mid = cur.lastrowid
        handle_row = exec(conn, "SELECT handle FROM users WHERE id=?",
                          (uid,)).fetchone()
        handle = handle_row["handle"] if handle_row else "unknown"
        # --- Daily streak bonus: first post of the day earns extra points ---
        # base 10 + streak*2, capped at +20 — rewards consistency without
        # making posting spam worthwhile.
        bonus = 0
        first_today = _count(conn,
            "SELECT COUNT(*) FROM room_messages WHERE user_id=? AND created_at>=?",
            (uid, _day_start_iso())) == 1
        if first_today:
            streak, _at_risk, _run = _compute_streak(conn, uid)
            bonus = min(10 + streak * 2, 20)
            exec(conn,
                "UPDATE users SET points_awarded = COALESCE(points_awarded,0) + ? "
                "WHERE id=?", (bonus, uid))
            conn.commit()
        # --- Double Points boost: +PTS_POST extra per post while active ---
        boosted = False
        brow = exec(conn, "SELECT boost_until FROM users WHERE id=?",
                    (uid,)).fetchone()
        now_iso = datetime.now(timezone.utc).isoformat()
        if brow and brow["boost_until"] and brow["boost_until"] > now_iso:
            exec(conn,
                "UPDATE users SET points_awarded = COALESCE(points_awarded,0) + ? "
                "WHERE id=?", (PTS_POST, uid))
            conn.commit()
            boosted = True
        points = _compute_points(conn, uid)  # keep leaderboard/points alive
        # Fire push notifications to everyone else (fire-and-forget; the
        # thread only does HTTP to the push service — never touches this conn).
        try:
            rows = exec(conn,
                "SELECT endpoint, keys FROM push_subs WHERE user_id <> ?",
                (uid,)).fetchall()
            if rows:
                subs = [{"endpoint": r["endpoint"], "keys": json.loads(r["keys"])}
                        for r in rows]
                payload = json.dumps({
                    "title": "💬 Whisper Room",
                    "body": f"@{handle}: {text[:80]}",
                    "tag": "room",
                    "url": "/",
                }).encode("utf-8")
                import threading
                threading.Thread(target=_push_to_all,
                                 args=(subs, payload), daemon=True).start()
        except Exception:
            pass  # push is best-effort; never break message posting
        conn.close()
        return jsonify({
            "id": mid,
            "sender_id": uid,
            "sender_handle": handle,
            "text": text,
            "created_at": created_at,
            "points": points,
            "bonus": bonus,
            "boosted": boosted,
        }), 201

    # === End Chat section ===

    # === Push notifications (Web Push / VAPID) ===

    @app.get("/api/push/config")
    def push_config():
        """Public VAPID key so the browser can subscribe. Auth-free: the
        applicationServerKey is public by design."""
        enabled = bool(app.config.get("VAPID_PUBLIC_KEY")
                       and app.config.get("VAPID_PRIVATE_KEY"))
        return jsonify({
            "enabled": enabled,
            "vapid_public": app.config.get("VAPID_PUBLIC_KEY") or None,
        })

    @app.post("/api/push/subscribe")
    def push_subscribe():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        endpoint = (p.get("endpoint") or "").strip()
        keys = p.get("keys") or {}
        if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
            return jsonify({"error": "Invalid subscription."}), 400
        keys_json = json.dumps({"p256dh": keys["p256dh"], "auth": keys["auth"]})
        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        import psycopg
        if isinstance(conn, psycopg.Connection):
            exec(conn,
                "INSERT INTO push_subs (user_id, endpoint, keys, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT (endpoint) DO UPDATE SET "
                "user_id=EXCLUDED.user_id, keys=EXCLUDED.keys, "
                "created_at=EXCLUDED.created_at",
                (session["user_id"], endpoint, keys_json, now))
        else:
            exec(conn,
                "INSERT OR REPLACE INTO push_subs "
                "(user_id, endpoint, keys, created_at) VALUES (?,?,?,?)",
                (session["user_id"], endpoint, keys_json, now))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe():
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        p = request.get_json(silent=True) or {}
        endpoint = (p.get("endpoint") or "").strip()
        if not endpoint:
            return jsonify({"error": "Endpoint required."}), 400
        conn = get_db()
        exec(conn, "DELETE FROM push_subs WHERE endpoint=? AND user_id=?",
             (endpoint, session["user_id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    # Schema is ensured lazily in get_db() on the first DB connection
    # (init_db(conn=...) after connect). Running it in a background thread
    # here shared the psycopg connection with request threads — psycopg3
    # connections are not thread-safe, and concurrent use deadlocked
    # requests for 60s (gunicorn timeout). Deploys still boot fast because
    # create_app() itself never touches the DB.

    app.logger.info("Campus Whispers: app started successfully")
    return app


def _render_digest(rows):
    """Plain-text digest of top rumors (external trigger email body)."""
    lines = ["Campus Whispers — today's top whispers", "=" * 36, ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. @{r['handle']}: {r['text']}")
    lines.append("")
    lines.append("What's the latest secret? Open Campus Whispers.")
    return "\n".join(lines)


def _send_digest_smtp(app, to_addrs, body):
    """Real SMTP send via smtplib. Config via env: DIGEST_SMTP_HOST,
    DIGEST_SMTP_PORT, DIGEST_SMTP_USER, DIGEST_SMTP_PASS, DIGEST_FROM.
    Returns count sent; returns 0 (and logs) if not configured."""
    import smtplib
    from email.message import EmailMessage
    host = app.config.get("DIGEST_SMTP_HOST") or os.environ.get("DIGEST_SMTP_HOST")
    if not host:
        app.logger.warning("Campus Whispers: digest SMTP not configured; "
                           "no emails sent.")
        return 0
    port = int(app.config.get("DIGEST_SMTP_PORT")
               or os.environ.get("DIGEST_SMTP_PORT") or 587)
    user = app.config.get("DIGEST_SMTP_USER") or os.environ.get("DIGEST_SMTP_USER")
    pwd = app.config.get("DIGEST_SMTP_PASS") or os.environ.get("DIGEST_SMTP_PASS")
    frm = app.config.get("DIGEST_FROM") or os.environ.get("DIGEST_FROM") \
        or (user or "noreply@campus-whispers.app")
    sent = 0
    try:
        with smtplib.SMTP(host, port) as s:
            if user:
                s.starttls()
                s.login(user, pwd)
            for to in to_addrs:
                msg = EmailMessage()
                msg["Subject"] = "Campus Whispers — today's top whispers"
                msg["From"] = frm
                msg["To"] = to
                msg.set_content(body)
                s.send_message(msg)
                sent += 1
    except Exception as exc:  # pragma: no cover - network path
        app.logger.error("Campus Whispers: digest SMTP failed: %s", exc)
    return sent


def rumor_public(row, conn=None):
    """Build the public dict for a rumor row. Row must carry handle, custom_alias,
    highlighted, is_incognito (or we query them when conn is given)."""
    # Safely get optional columns (sqlite3.Row doesn't have .get())
    try:
        custom_alias = row["custom_alias"] if row["custom_alias"] else None
    except (KeyError, IndexError, TypeError):
        custom_alias = None
    try:
        is_inc = int(row["is_incognito"])
    except (KeyError, IndexError, TypeError, ValueError):
        is_inc = 0
    try:
        hl = int(row["highlighted"])
    except (KeyError, IndexError, TypeError, ValueError):
        hl = 0

    handle = custom_alias if custom_alias else row["handle"]
    if is_inc:
        handle = "👻 Ghost"
        hl = 0  # incognito posts shouldn't glow
    try:
        bumped_recent = _is_recent(row["bumped_at"], 1)
    except (KeyError, IndexError, TypeError, ValueError):
        bumped_recent = False
    data = {"id": row["id"], "text": row["text"],
            "created_at": row["created_at"], "handle": handle,
            "highlighted": hl,
            "user_id": row["user_id"],
            "bumped": bumped_recent}
    if conn is not None:
        data["tags"] = _rumor_tags(conn, row["id"])
        # Variable-reward surprise: is this post featured?
        frow = exec(conn, "SELECT featured FROM rumors WHERE id=?",
                    (row["id"],)).fetchone()
        data["featured"] = int(frow["featured"]) if frow and frow["featured"] is not None else 0
        # Badge flair: user's selected badge, or highest earned
        try:
            uid = row["user_id"]
            # Read user's selected badge preference
            srow = exec(conn,
                "SELECT selected_badge FROM users WHERE id=?", (uid,)
            ).fetchone()
            selected = (srow["selected_badge"] if srow and srow["selected_badge"] else None)
            badge_label = None
            if selected:
                # purchased badge
                if selected in SHOP_ITEMS:
                    badge_label = SHOP_ITEMS[selected][0]
                # earned milestone badge
                if not badge_label:
                    for key, label, threshold in BADGE_DEFS:
                        if key == selected:
                            badge_label = label
                            break
            if not badge_label:
                posts = _count(conn, "SELECT COUNT(*) FROM rumors WHERE user_id=?", (uid,))
                for key, label, threshold in reversed(BADGE_DEFS):
                    if posts >= threshold:
                        badge_label = label
                        break
            data["badge_label"] = badge_label
        except Exception:
            pass
    # Rising-star gentle floor: flag posts < 24h old so the UI can soften blank-slate
    data["is_new"] = _is_new(row["created_at"])
    return data


def _is_recent(iso_ts, hours):
    """True if the timestamp is within the last `hours` hours."""
    from datetime import datetime, timezone, timedelta
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) < timedelta(hours=hours)
    except Exception:
        return False


def _is_new(created_at):
    """True if the post is less than 24h old."""
    return _is_recent(created_at, 24)



def _rumor_tags(conn, rumor_id):
    rows = exec(conn,
        "SELECT t.name FROM rumor_tags rt JOIN tags t ON t.id = rt.tag_id "
        "WHERE rt.rumor_id=?", (rumor_id,)).fetchall()
    return [r["name"] for r in rows]


def _upsert_tag(conn, name):
    """Insert a tag if absent; return its id (cross-DB safe)."""
    if _conn_is_pg(conn):
        cur = exec(conn,
            "INSERT INTO tags (name) VALUES (?) "
            "ON CONFLICT (name) DO NOTHING RETURNING id", (name,))
        row = cur.fetchone()
        if row:
            return row["id"]
        return exec(conn, "SELECT id FROM tags WHERE name=?",
                    (name,)).fetchone()["id"]
    exec(conn, "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    return exec(conn, "SELECT id FROM tags WHERE name=?",
               (name,)).fetchone()["id"]



def _make_teaser(text):
    """Information-gap teaser: open a curiosity gap without revealing text.

    Shows a short masked fragment + ellipsis so readers feel a knowledge
    gap (Loewenstein) that pulls them to open the full rumor.
    """
    words = text.split()
    if len(words) <= 4:
        return "🤫 " + "•" * len(text) + "…"
    frag = " ".join(words[:4])
    return f"🤫 {frag}…"


def _conn_is_pg(conn):
    import psycopg
    return isinstance(conn, psycopg.Connection)


def _compute_streak(conn, user_id):
    """Return (streak, at_risk_today, run).

    Streak = consecutive distinct UTC calendar days with >=1 post, ending
    today or yesterday (1-day grace window so a single missed day doesn't
    shatter the streak — recovery prevents churn per Nikzad 2021).
    at_risk_today is True when the last post was yesterday: today is the
    final grace day to keep the streak alive (loss-aversion trigger).
    run = the consecutive-day run ending at the most recent active day
    (used by the Streak Shield to restore a streak after a deeper miss).
    """
    from datetime import datetime, timezone, date, timedelta
    rows = exec(conn,
        "SELECT DISTINCT substr(created_at,1,10) AS d FROM rumors "
        "WHERE user_id=? UNION "
        "SELECT DISTINCT substr(created_at,1,10) AS d FROM room_messages "
        "WHERE user_id=? ORDER BY d DESC", (user_id, user_id)).fetchall()
    if not rows:
        return 0, False, 0
    dates = [r["d"] for r in rows]
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    # group into consecutive-day runs from most recent backward
    run = 1
    for prev, cur in zip(dates, dates[1:]):
        d_prev = date.fromisoformat(prev)
        d_cur = date.fromisoformat(cur)
        if (d_prev - d_cur).days == 1:
            run += 1
        else:
            break
    most_recent = date.fromisoformat(dates[0])
    preserved_run = run  # streak as of the last active day (Streak Shield)
    at_risk = False
    if most_recent == today:
        at_risk = False
    elif most_recent == yesterday:
        at_risk = True
    else:
        run = 0
    return run, at_risk, preserved_run


def rumor_admin(row):
    return {"id": row["id"], "text": row["text"],
            "created_at": row["created_at"], "handle": row["handle"],
            "real_name": row["real_name"]}


def serve_page(name):
    path = os.path.join(os.path.dirname(__file__), "static", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

_db_global = None
# Cached Postgres IP (avoids re-resolving; the internal DNS on some Render
# instances hangs for minutes, so we pin the IP once we have it).
_pg_ip_cache = None
_PG_IP_FILE = os.path.join(os.path.sep, "tmp", "pg_ip.txt")


def _resolve_pg_ip_bounded(url, timeout=3.0):
    """Resolve a Postgres hostname to an IPv4 with a hard timeout.

    socket.getaddrinfo() on some Render instances hangs for many minutes
    (flaky internal DNS). Running it in a daemon thread with a bounded join
    means we never block startup or requests on DNS.
    Returns the IP string, or None on timeout/failure.
    """
    try:
        from urllib.parse import urlsplit
    except Exception:
        return None
    try:
        host = urlsplit(url).hostname
    except Exception:
        return None
    if not host:
        return None
    box = []

    def _do():
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            if infos:
                box.append(infos[0][4][0])
        except Exception:
            pass

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


def _pg_connect_bounded(url, timeout=12.0):
    """psycopg.connect() with a hard overall timeout.

    connect_timeout=5 in the URL does not reliably cover DNS resolution
    (libpq applies it after name resolution), and a hung resolver would
    block a request forever. Connecting in a daemon thread with a bounded
    join guarantees we return (or raise) within `timeout` seconds.
    """
    import psycopg
    from psycopg.rows import dict_row
    box = {}

    def _do():
        try:
            box["conn"] = psycopg.connect(url, row_factory=dict_row)
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout)
    if "conn" in box:
        return box["conn"]
    if "err" in box:
        raise box["err"]
    raise TimeoutError(f"Postgres connect timed out after {timeout}s")


def _pg_url_with_ip(url, ip):
    """Replace the hostname in a Postgres URL with an IP literal, forcing
    sslmode=require (no hostname verification — we're connecting by IP)."""
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return url
        netloc = parts.netloc.replace(host, ip)
        query = parts.query
        if "sslmode=" not in query:
            query = (query + "&" if query else "") + "sslmode=require"
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        return url


def get_db(db_path=None):
    """Return a connection to whichever DB is configured.

    If DATABASE_URL is set, keep a single persistent psycopg connection
    (reconnecting transparently); otherwise fall back to local SQLite.
    """

    # Cache connection per request
    if db_path is None and 'db' in g:
        return g.db
    url = db_path or current_app.config.get("DATABASE_URL")
    if url:
        try:
            import psycopg

            if db_path is None:
                global _db_global, _pg_ip_cache
                if _db_global is None:
                    # Pin the host to an IP so a flaky DNS resolver can't
                    # hang the connect (the bounded resolve returns None
                    # quickly on timeout; we then fall back to the hostname).
                    ip = _pg_ip_cache
                    if not ip and os.path.exists(_PG_IP_FILE):
                        try:
                            ip = open(_PG_IP_FILE).read().strip() or None
                        except Exception:
                            ip = None
                    if not ip:
                        ip = _resolve_pg_ip_bounded(url)
                        if ip:
                            _pg_ip_cache = ip
                            try:
                                with open(_PG_IP_FILE, "w") as f:
                                    f.write(ip)
                            except Exception:
                                pass
                    conn_url = _pg_url_with_ip(url, ip) if ip else url
                    _db_global = _pg_connect_bounded(conn_url)
                    # Patch close() to no-op — teardown just pops g.db
                    _db_global._orig_close = _db_global.close
                    _db_global.close = lambda: None
                    # Ensure schema on this fresh connection (single worker,
                    # so no concurrent access; the old background thread
                    # shared the conn with requests and deadlocked psycopg).
                    init_db(conn=_db_global)
                else:
                    # Quick ping — if it fails, reconnect once
                    try:
                        _db_global.cursor().execute("SELECT 1").fetchone()
                    except Exception:
                        try:
                            _db_global._orig_close()
                        except Exception:
                            pass
                        ip = _pg_ip_cache
                        if not ip and os.path.exists(_PG_IP_FILE):
                            try:
                                ip = open(_PG_IP_FILE).read().strip() or None
                            except Exception:
                                ip = None
                        conn_url = _pg_url_with_ip(url, ip) if ip else url
                        _db_global = _pg_connect_bounded(conn_url)
                        _db_global._orig_close = _db_global.close
                        _db_global.close = lambda: None
                g.db = _db_global
                return _db_global
            else:
                # One-off connection (init_db with custom path)
                from psycopg.rows import dict_row
                return psycopg.connect(
                    f"{url}{'&' if '?' in url else '?'}connect_timeout=5",
                    row_factory=dict_row)

        except ImportError:
            pass  # no psycopg → SQLite fallback
    # SQLite (local dev)
    conn = sqlite3.connect(
        current_app.config["DB_PATH"], timeout=5
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if db_path is None:
        g.db = conn
        init_db(conn=conn)  # ensure local schema (startup init was removed)
    return conn


def hash_password(pw):
    return generate_password_hash(pw)


def exec(conn, sql, params=()):
    import psycopg
    if isinstance(conn, psycopg.Connection):
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


def init_db(db_path=None, conn=None):
    conn = conn or get_db(db_path)
    if isinstance(conn, sqlite3.Connection):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                real_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                handle TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                banned INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rumors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                bumped_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                rumor_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                UNIQUE(user_id, rumor_id, kind),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (rumor_id) REFERENCES rumors(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS me_too (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                rumor_id INTEGER NOT NULL,
                UNIQUE(user_id, rumor_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (rumor_id) REFERENCES rumors(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                rumor_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (rumor_id) REFERENCES rumors(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rumor_tags (
                rumor_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (rumor_id, tag_id),
                FOREIGN KEY (rumor_id) REFERENCES rumors(id),
                FOREIGN KEY (tag_id) REFERENCES tags(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tag_follows (
                user_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, tag_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (tag_id) REFERENCES tags(id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS challenge_claims (
                user_id INTEGER NOT NULL,
                challenge_key TEXT NOT NULL,
                week TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, challenge_key, week),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )"""
        )
        # Bump metadata
        try:
            conn.execute("ALTER TABLE rumors ADD COLUMN bumped_at TEXT")
        except Exception:
            pass
        # Variable-reward surprise: featured flag
        try:
            conn.execute("ALTER TABLE rumors ADD COLUMN featured INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # Shop columns
        for col, typ in [("highlighted", "INTEGER NOT NULL DEFAULT 0"),
                         ("is_incognito", "INTEGER NOT NULL DEFAULT 0")]:
            try:
                conn.execute(f"ALTER TABLE rumors ADD COLUMN {col} {typ}")
            except Exception:
                pass
        for col, typ in [("points", "INTEGER NOT NULL DEFAULT 0"),
                         ("points_spent", "INTEGER NOT NULL DEFAULT 0"),
                         ("points_awarded", "INTEGER NOT NULL DEFAULT 0"),
                         ("custom_alias", "TEXT"),
                         ("name_color", "TEXT"),
                         ("name_color_until", "TEXT"),
                         ("streak_shield", "INTEGER NOT NULL DEFAULT 0"),
                         ("boost_until", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
            except Exception:
                pass
        conn.execute(
            """CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL,
                rumor_id INTEGER,
                meta TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )"""
        )
        # Self-heal: add selected_badge column for SQLite
        try:
            conn.execute("ALTER TABLE users ADD COLUMN selected_badge TEXT DEFAULT NULL")
        except Exception:
            pass
        # Chat tables
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversation_participants (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                PRIMARY KEY (conversation_id, user_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                sender_id INTEGER NOT NULL REFERENCES users(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        # Group room (the feed is now a shared group chat)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS room_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        # One-time seed: migrate existing rumors into the room as history
        conn.execute(
            """INSERT INTO room_messages (user_id, text, created_at)
               SELECT user_id, text, created_at FROM rumors
               WHERE NOT EXISTS (SELECT 1 FROM room_messages)"""
        )
        # Room-message shop effects (self-healing column adds)
        for col, typ in [("highlighted", "INTEGER NOT NULL DEFAULT 0"),
                         ("is_incognito", "INTEGER NOT NULL DEFAULT 0"),
                         ("pinned_until", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE room_messages ADD COLUMN {col} {typ}")
            except Exception:
                pass
        # Push notification subscriptions
        conn.execute(
            """CREATE TABLE IF NOT EXISTS push_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                endpoint TEXT NOT NULL UNIQUE,
                keys TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
    else:  # Postgres
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                real_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                handle TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                banned INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # Self-heal: add email column if it's missing.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE")
        except Exception:
            pass
        # Self-heal: add selected_badge column
        try:
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS selected_badge TEXT DEFAULT NULL")
        except Exception:
            pass
        # Self-heal: shop/economy columns
        for col, typ in [("points", "INTEGER NOT NULL DEFAULT 0"),
                         ("points_spent", "INTEGER NOT NULL DEFAULT 0"),
                         ("points_awarded", "INTEGER NOT NULL DEFAULT 0"),
                         ("custom_alias", "TEXT"),
                         ("name_color", "TEXT"),
                         ("name_color_until", "TEXT"),
                         ("streak_shield", "INTEGER NOT NULL DEFAULT 0"),
                         ("boost_until", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rumors (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                bumped_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                rumor_id INTEGER NOT NULL REFERENCES rumors(id),
                kind TEXT NOT NULL,
                UNIQUE(user_id, rumor_id, kind)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS me_too (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                rumor_id INTEGER NOT NULL REFERENCES rumors(id),
                UNIQUE(user_id, rumor_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                rumor_id INTEGER NOT NULL REFERENCES rumors(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tags (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rumor_tags (
                rumor_id INTEGER NOT NULL REFERENCES rumors(id),
                tag_id INTEGER NOT NULL REFERENCES tags(id),
                PRIMARY KEY (rumor_id, tag_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tag_follows (
                user_id INTEGER NOT NULL REFERENCES users(id),
                tag_id INTEGER NOT NULL REFERENCES tags(id),
                PRIMARY KEY (user_id, tag_id)
            )"""
        )
        # Bump metadata
        try:
            conn.execute("ALTER TABLE rumors ADD COLUMN IF NOT EXISTS bumped_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE rumors ADD COLUMN IF NOT EXISTS featured INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        for col in ["highlighted", "is_incognito"]:
            try:
                conn.execute(f"ALTER TABLE rumors ADD COLUMN IF NOT EXISTS {col} INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
        for col in ["points", "points_spent", "points_awarded"]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_alias TEXT")
        except Exception:
            pass
        conn.execute(
            """CREATE TABLE IF NOT EXISTS challenge_claims (
                user_id INTEGER NOT NULL REFERENCES users(id),
                challenge_key TEXT NOT NULL,
                week TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, challenge_key, week)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL,
                rumor_id INTEGER,
                meta TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )"""
        )
        # Chat tables (Postgres)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversation_participants (
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                PRIMARY KEY (conversation_id, user_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                sender_id INTEGER NOT NULL REFERENCES users(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        # Group room (the feed is now a shared group chat)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS room_messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        # One-time seed: migrate existing rumors into the room as history
        conn.execute(
            """INSERT INTO room_messages (user_id, text, created_at)
               SELECT user_id, text, created_at FROM rumors
               WHERE NOT EXISTS (SELECT 1 FROM room_messages)"""
        )
        # Room-message shop effects (self-healing column adds)
        for col, typ in [("highlighted", "INTEGER NOT NULL DEFAULT 0"),
                         ("is_incognito", "INTEGER NOT NULL DEFAULT 0"),
                         ("pinned_until", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE room_messages ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass
        # Push notification subscriptions (Postgres)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS push_subs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                endpoint TEXT NOT NULL UNIQUE,
                keys TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
    conn.commit()
    # Don't close pool connections — the teardown handler returns them
    # (init_db is called inside app_context so close_db fires on exit)


def hash_password(pw):
    return generate_password_hash(pw)


def _generate_handle(conn):
    """Create a unique random handle (privacy: user doesn't pick/see it)."""
    import random, string
    while True:
        slug = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        handle = f"anon_{slug}"
        exists = exec(conn, "SELECT 1 FROM users WHERE handle=?",
                     (handle,)).fetchone()
        if not exists:
            return handle


# ============================================================
# Reward system (research-grounded — see engagement-feature-design skill)
# Points are the engine (correlate with all engagement types); badges are
# decoration; challenges refresh weekly (beat novelty wear-off); leaderboard
# is anonymized (privacy prerequisite for an anon board).
# ============================================================

# Points economy (kept small + legible).
PTS_POST = 10
PTS_REACT_GIVEN = 1
PTS_REACT_RECEIVED = 2
PTS_METOO_RECEIVED = 2

# Milestone badges: key -> (label, threshold on post count) or custom.
BADGE_DEFS = [  # (key, label, post threshold)
    ("first_whisper", "First Whisper", 1),
    ("ten_whispers", "10 Whispers", 10),
    ("twenty_five", "Whisper Enthusiast", 25),
    ("fifty_whispers", "50 Whispers", 50),
    ("hundred_whispers", "Century Club", 100),
    ("two_hundred", "Whisper Legend", 200),
    ("five_hundred", "Anonymous Icon", 500),
]

# Weekly + daily challenges: key -> (label, goal, reward points, kind).
# kind "post" = this week's room messages; kind "daily" = today's messages.
CHALLENGE_DEFS = [
    ("post_3", "Post 3 messages this week", 3, 30, "post"),
    ("post_10", "Post 10 messages this week", 10, 80, "post"),
    ("daily_3", "Post 3 messages today", 3, 30, "daily"),
]

# Name-color whitelist (never render arbitrary CSS from the client).
NAME_COLORS = ["#FF6B6B", "#FFA94D", "#FFD43B", "#69DB7C", "#4DABF7",
               "#9775FA", "#F783AC", "#20C997", "#FF922B", "#748FFC"]

# Mystery Box prize table: (cumulative weight, pts, label). 96% points,
# 4% a free purchasable flair badge.
MYSTERY_PRIZES = [
    (0.40, 30, "30 pts"),
    (0.25, 60, "60 pts"),
    (0.15, 100, "100 pts"),
    (0.10, 150, "150 pts"),
    (0.06, 250, "250 pts"),
    (0.04, 0, "BADGE"),
]

# Shop: item key -> (label, description, price).
SHOP_ITEMS = {
    "alias": ("✏️ Custom Alias", "Set a custom display name on your messages (max 30 chars)", 80),
    "name_color": ("🎨 Name Color", "Colored handle in the room for 30 days", 60),
    "highlight": ("✨ Highlight Message", "Glowing border on one of your room messages", 50),
    "ghost": ("👻 Ghost Message", "One room message with no handle — ghost mode", 150),
    "pin": ("📌 Pin to Top", "Pin one of your messages to the top of the room for 24h", 100),
    "streak_shield": ("🧊 Streak Shield", "Survives one missed day without breaking your streak", 80),
    "boost": ("🚀 Double Points", "2× points on every post for the next 24h", 120),
    "mystery_box": ("🎁 Mystery Box", "Random prize: 30–250 pts or a free flair badge", 90),
    "featured": ("👑 Featured Spot", "Featured badge on your profile for 1 week", 200),
    "badge_smooth": ("Smooth Talker 🎩", "Equip the Smooth Talker badge as your flair", 100),
    "badge_mystery": ("Mystery Guest 🎭", "Equip the Mystery Guest badge as your flair", 150),
    "badge_veteran": ("Veteran 👑", "Equip the Veteran badge as your flair", 200),
    "badge_crown": ("Crown Royal 👑", "Equip the Crown Royal badge as your flair", 300),
}

# Items that target one of your own room messages.
MESSAGE_ITEMS = {"highlight", "ghost", "pin"}


def _current_week():
    """ISO year-week string, e.g. '2026-W29' — used to scope challenges."""
    from datetime import datetime, timezone
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_start_iso():
    """UTC midnight of Monday this week, ISO string for created_at compares."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.isoformat()


def _day_start_iso():
    """UTC midnight today, ISO string for created_at compares."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _challenge_scope(kind):
    """Claim scope: weekly challenges reset per ISO week, daily per calendar day."""
    from datetime import date
    return _current_week() if kind != "daily" else date.today().isoformat()


def _count(conn, sql, params):
    row = exec(conn, sql, params).fetchone()
    if not row:
        return 0
    # row may be a dict-like (psycopg dict_row) or sqlite3.Row/tuple
    try:
        if isinstance(row, dict):
            vals = list(row.values())
        else:
            vals = list(row)
        return int(vals[0]) if vals else 0
    except Exception:
        return 0


def _push_to_all(subscriptions, payload):
    """Send a push payload to a list of subscriptions (HTTP only — never
    touches the DB, safe to run in a thread). Best-effort: failures are
    swallowed; expired subscriptions (404/410) are simply skipped."""
    from flask import current_app
    priv = current_app.config.get("VAPID_PRIVATE_KEY") or ""
    subj = current_app.config.get("VAPID_SUBJECT") or ""
    if not priv:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return
    body = payload if isinstance(payload, str) else payload.decode("utf-8")
    for sub in subscriptions:
        try:
            webpush(sub, body, vapid_private_key=priv,
                    vapid_claims={"sub": subj}, ttl=86400)
        except WebPushException:
            pass  # 404/410 = subscription gone; others = service hiccup
        except Exception:
            pass


def _compute_points(conn, user_id):
    """Sum a user's points from their activity across existing tables."""
    posts = _count(conn, "SELECT COUNT(*) FROM rumors WHERE user_id=?", (user_id,))
    react_given = _count(conn, "SELECT COUNT(*) FROM reactions WHERE user_id=?", (user_id,))
    react_recv = _count(conn,
        "SELECT COUNT(*) FROM reactions rx JOIN rumors r ON r.id=rx.rumor_id "
        "WHERE r.user_id=?", (user_id,))
    metoo_recv = _count(conn,
        "SELECT COUNT(*) FROM me_too m JOIN rumors r ON r.id=m.rumor_id "
        "WHERE r.user_id=?", (user_id,))
    room_posts = _count(conn,
        "SELECT COUNT(*) FROM room_messages WHERE user_id=?", (user_id,))
    # claimed challenge rewards
    claim_pts = 0
    rows = exec(conn,
        "SELECT challenge_key FROM challenge_claims WHERE user_id=?",
        (user_id,)).fetchall()
    reward_by_key = {c[0]: c[3] for c in CHALLENGE_DEFS}
    for r in rows:
        key = r[0] if not hasattr(r, "keys") else r["challenge_key"]
        claim_pts += reward_by_key.get(key, 0)
    earned = ((posts + room_posts) * PTS_POST +
              react_given * PTS_REACT_GIVEN + react_recv * PTS_REACT_RECEIVED +
              metoo_recv * PTS_METOO_RECEIVED + claim_pts)
    # admin-awarded bonus points
    row_aw = exec(conn, "SELECT points_awarded FROM users WHERE id=?", (user_id,)).fetchone()
    if row_aw is None:
        return 0
    awarded = row_aw[0] if not hasattr(row_aw, "keys") else row_aw["points_awarded"]
    # subtract points spent in the shop
    row = exec(conn, "SELECT points_spent FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return 0
    spent = row[0] if not hasattr(row, "keys") else row["points_spent"]
    total = max(earned + awarded - spent, 0)
    exec(conn, "UPDATE users SET points=? WHERE id=?", (total, user_id))
    # Commit the cache write here — the leaderboard and rank queries read
    # users.points, and without a commit they see stale zeros on both DBs.
    conn.commit()
    return total


def _compute_badges(conn, user_id):
    """Return unlocked badges (list of {key,label}) — earned + purchased."""
    posts = _count(conn, "SELECT COUNT(*) FROM rumors WHERE user_id=?",
                   (user_id,))
    room_posts = _count(conn, "SELECT COUNT(*) FROM room_messages WHERE user_id=?",
                        (user_id,))
    out = []
    for key, label, threshold in BADGE_DEFS:
        if posts + room_posts >= threshold:
            out.append({"key": key, "label": label})
    # Include purchased flair badges
    rows = exec(conn,
        "SELECT meta FROM purchases WHERE user_id=? AND kind='badge'",
        (user_id,)).fetchall()
    for r in rows:
        k = r[0] if not hasattr(r, "keys") else r["meta"]
        if k in SHOP_ITEMS:
            label = SHOP_ITEMS[k][0]
            out.append({"key": k, "label": label})
    return out


def _challenge_progress(conn, user_id, kind):
    """Count this-week (or today's) activity for a challenge kind."""
    if kind == "post":
        ws = _week_start_iso()
        return _count(conn,
            "SELECT COUNT(*) FROM rumors WHERE user_id=? AND created_at>=?",
            (user_id, ws)) + _count(conn,
            "SELECT COUNT(*) FROM room_messages WHERE user_id=? AND created_at>=?",
            (user_id, ws))
    if kind == "daily":
        ds = _day_start_iso()
        return _count(conn,
            "SELECT COUNT(*) FROM room_messages WHERE user_id=? AND created_at>=?",
            (user_id, ds))
    if kind == "react":
        # reactions table has no created_at; count all this user's reactions
        return _count(conn, "SELECT COUNT(*) FROM reactions WHERE user_id=?",
                      (user_id,))
    return 0


def _user_rank(conn, user_id):
    """1-based rank of a user by points (higher points = better rank)."""
    row = exec(conn,
        "SELECT COUNT(*) + 1 AS user_rank FROM users WHERE banned=0 AND points > "
        "(SELECT points FROM users WHERE id=?)",
        (user_id,)).fetchone()
    return row["user_rank"] if row else None
"""Local read-only dashboard over the archive database.

Runs on your machine only. Nothing here talks to Discord; it just reads SQLite.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template, request,
                   send_file)


def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path)

    def conn():
        """sqlite3's own context manager only commits/rolls back - it does NOT
        close the connection. Wrap in closing() so `with conn() as c` releases
        the file handle."""
        c = sqlite3.connect(app.config["DB_PATH"])
        c.row_factory = sqlite3.Row
        return closing(c)

    def attach_attachments(c, rows):
        """Attach each message's attachment rows so the UI can render images
        and file links without an extra round-trip per message."""
        ids = [r["id"] for r in rows]
        if not ids:
            return [dict(r) | {"attachments": []} for r in rows]
        ph = ",".join("?" * len(ids))
        atts = c.execute(
            f"SELECT id, message_id, filename, url, local_path, content_type, "
            f"width, height, download_status FROM attachments "
            f"WHERE message_id IN ({ph})", ids).fetchall()
        by: dict = {}
        for a in atts:
            by.setdefault(a["message_id"], []).append(dict(a))
        return [dict(r) | {"attachments": by.get(r["id"], [])} for r in rows]

    @app.route("/")
    def index():
        with conn() as c:
            guilds = c.execute("SELECT * FROM guilds ORDER BY name").fetchall()
            channels = c.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id) AS n "
                "FROM channels c ORDER BY c.position, c.id"
            ).fetchall()
            stats = c.execute(
                "SELECT COUNT(*) AS messages, SUM(deleted) AS deleted FROM messages"
            ).fetchone()
            att = c.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN download_status='done' THEN 1 ELSE 0 END) AS done "
                "FROM attachments"
            ).fetchone()
        return render_template(
            "index.html",
            guilds=guilds, channels=channels,
            messages=stats["messages"] or 0,
            deleted=stats["deleted"] or 0,
            attachments=att["n"] or 0,
            attachments_done=att["done"] or 0,
        )

    @app.route("/api/guilds")
    def api_guilds():
        with conn() as c:
            rows = c.execute("SELECT * FROM guilds ORDER BY name").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/stats")
    def api_stats():
        with conn() as c:
            m = c.execute("SELECT COUNT(*) n, SUM(deleted) d FROM messages").fetchone()
            a = c.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN download_status='done' THEN 1 ELSE 0 END) done, "
                "SUM(CASE WHEN download_status='done' THEN size ELSE 0 END) bytes "
                "FROM attachments"
            ).fetchone()
            ch = c.execute("SELECT COUNT(*) n FROM channels").fetchone()
            mem = c.execute("SELECT COUNT(*) n FROM members").fetchone()
        return jsonify({
            "messages": m["n"] or 0, "deleted": m["d"] or 0, "channels": ch["n"],
            "members": mem["n"], "attachments": a["n"] or 0,
            "attachments_downloaded": a["done"] or 0, "attachment_bytes": a["bytes"] or 0,
        })

    @app.route("/api/channels")
    def api_channels():
        with conn() as c:
            rows = c.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) AS n "
                "FROM channels c ORDER BY c.position, c.id"
            ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/members")
    def api_members():
        # Annotate each member with their highest-position role so the UI can
        # group them Discord-style (role headers in hierarchy order).
        with conn() as c:
            rows = c.execute(
                "SELECT m.*, "
                "(SELECT r.name FROM member_roles mr JOIN roles r ON r.id=mr.role_id "
                " WHERE mr.member_id=m.id ORDER BY r.position DESC LIMIT 1) AS top_role, "
                "(SELECT r.position FROM member_roles mr JOIN roles r ON r.id=mr.role_id "
                " WHERE mr.member_id=m.id ORDER BY r.position DESC LIMIT 1) AS top_pos, "
                "(SELECT r.colour FROM member_roles mr JOIN roles r ON r.id=mr.role_id "
                " WHERE mr.member_id=m.id ORDER BY r.position DESC LIMIT 1) AS top_colour "
                "FROM members m ORDER BY top_pos DESC, m.name").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/roles")
    def api_roles():
        with conn() as c:
            rows = c.execute(
                "SELECT * FROM roles ORDER BY position DESC, name").fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/audit")
    def api_audit():
        limit = min(int(request.args.get("limit", 100)), 500)
        with conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/search")
    def api_search():
        q = (request.args.get("q") or "").strip()
        channel_id = request.args.get("channel")
        author = request.args.get("author")
        limit = min(int(request.args.get("limit", 100)), 500)
        if not q and not author:
            return jsonify([])

        sql = ("SELECT m.*, c.name AS channel_name FROM messages m "
               "LEFT JOIN channels c ON c.id = m.channel_id WHERE 1=1")
        params: list = []
        if q:
            sql += " AND m.content LIKE ?"
            params.append(f"%{q}%")
        if channel_id:
            sql += " AND m.channel_id = ?"
            params.append(channel_id)
        if author:
            sql += " AND (m.author_name LIKE ? OR m.author_id = ?)"
            params.extend([f"%{author}%", author])
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)

        with conn() as c:
            rows = c.execute(sql, params).fetchall()
            out = attach_attachments(c, rows)
        return jsonify(out)

    @app.route("/api/channel/<channel_id>")
    def api_channel(channel_id: str):
        before = request.args.get("before")
        limit = min(int(request.args.get("limit", 100)), 500)
        sql = "SELECT * FROM messages WHERE channel_id = ?"
        params: list = [channel_id]
        if before:
            sql += " AND id < ?"
            params.append(before)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with conn() as c:
            rows = c.execute(sql, params).fetchall()
            out = attach_attachments(c, rows)
        return jsonify(list(reversed(out)))

    @app.route("/export/<fmt>")
    def export(fmt: str):
        """Download an export built on demand from the live database."""
        if fmt not in ("json", "csv"):
            abort(404)

        from archiver import export as ex
        from archiver.db import Database

        db = Database(app.config["DB_PATH"])
        try:
            row = db.conn.execute("SELECT id, name FROM guilds LIMIT 1").fetchone()
            guild_id = row["id"] if row else None
            name = ((row["name"] if row else "archive") or "archive")
            name = name.replace(" ", "-").lower()
            out_dir = Path(app.config["DB_PATH"]).parent / "exports" / "dashboard"

            if fmt == "json":
                p = ex.export_json(db, out_dir / f"{name}.json", guild_id=guild_id)
                return Response(p.read_text(encoding="utf-8"),
                                mimetype="application/json",
                                headers={"Content-Disposition":
                                         f'attachment; filename="{p.name}"'})
            p = ex.export_csv(db, out_dir / f"{name}.csv", guild_id=guild_id)
            return Response(p.read_text(encoding="utf-8"), mimetype="text/csv",
                            headers={"Content-Disposition":
                                     f'attachment; filename="{p.name}"'})
        finally:
            db.close()

    @app.route("/file/<att_id>")
    def file_route(att_id: str):
        """Serve a locally-saved attachment so image messages can embed/preview
        it. local_path may be relative to the run directory; resolve it safely."""
        with conn() as c:
            row = c.execute(
                "SELECT local_path, filename, content_type FROM attachments "
                "WHERE id = ?", (att_id,)).fetchone()
        if not row or not row["local_path"]:
            abort(404)
        p = Path(row["local_path"]).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            abort(404)
        return send_file(p, mimetype=row["content_type"] or None,
                         download_name=row["filename"] or p.name)

    return app


if __name__ == "__main__":
    import sys
    create_app(sys.argv[1] if len(sys.argv) > 1 else "data/archive.sqlite3").run(
        host="0.0.0.0", port=8080, debug=True)

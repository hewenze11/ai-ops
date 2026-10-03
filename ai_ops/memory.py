"""Daily role memory with configurable injection tiers.

Requirements (user, 2026-10-02):
  * archive conversation by calendar day, per role (roles are isolated);
  * inject by age with NO overlapping day boundaries, e.g. "last 2 days full
    text, day 2..5 compressed, older summarised";
  * the model can load an old day's full text on demand via an interface;
  * memory is editable and deletable by an operator;
  * raw audit stays separate and is never derived from the model's memory.

Design:
  * The immutable source of truth is still ``role_turns`` + ``audit``. This
    module builds a per-(role, day) view on top of it.
  * Derived day rows live in ``role_memory``: one row per (role_id, day). A row
    keeps the concatenated transcript for that day plus a compressed and a
    summary form. Rows can be rebuilt from turns, or pinned by an operator edit
    (``edited_at`` set) which then wins over regeneration.
  * Injection policy is a set of ordered tiers, each with an age window in days
    and a form (full/compressed/summary). Windows are [start, end) so they never
    overlap; the tier list is validated to be gap-free and non-overlapping.
  * Injection reads only days already materialised. It never calls the model to
    build context (the summariser runs in the background), so a turn never pays
    a surprise model call just to assemble memory.
"""
import json
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS role_memory(
 role_id TEXT NOT NULL REFERENCES roles(id), day TEXT NOT NULL,
 full_text TEXT NOT NULL, compressed TEXT, summary TEXT,
 source TEXT NOT NULL, turn_count INTEGER NOT NULL DEFAULT 0,
 edited_at REAL, generated_at REAL NOT NULL,
 PRIMARY KEY(role_id, day));
CREATE TABLE IF NOT EXISTS role_memory_policy(
 role_id TEXT PRIMARY KEY REFERENCES roles(id), config TEXT NOT NULL);
"""

# The default policy encodes the user's example: two days full, then up to five
# days compressed, everything older as a summary. [start, end) in days of age;
# age 0 = today. Non-overlapping by construction.
DEFAULT_POLICY = {"tiers": [
    {"max_age_days": 2, "form": "full"},
    {"max_age_days": 5, "form": "compressed"},
    {"max_age_days": None, "form": "summary"},
]}
MAX_TIERS = 8


class PolicyError(ValueError):
    pass


def validate_policy(config):
    """Tiers must be strictly increasing, gap-free and non-overlapping.

    Each tier covers ages in (previous_max, max_age_days]. The first tier must
    start at 0 (previous_max = 0) and the last must be open-ended (None) so
    every age is covered exactly once.
    """
    tiers = config.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise PolicyError("policy.tiers must be a non-empty list")
    if len(tiers) > MAX_TIERS:
        raise PolicyError("too many tiers")
    previous = 0
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict) or set(tier) != {"max_age_days", "form"}:
            raise PolicyError("each tier needs exactly max_age_days and form")
        form = tier["form"]
        if form not in ("full", "compressed", "summary"):
            raise PolicyError("unknown form: " + str(form))
        max_age = tier["max_age_days"]
        last = index == len(tiers) - 1
        if last:
            if max_age is not None:
                raise PolicyError("the final tier must be open-ended (max_age_days null)")
            break
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= previous:
            raise PolicyError("tier bounds must be strictly increasing integers")
        previous = max_age
    return True


def policy_for(db, role_id):
    row = db.execute("SELECT config FROM role_memory_policy WHERE role_id=?", (role_id,)).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULT_POLICY))
    config = json.loads(row["config"])
    validate_policy(config)
    return config


def form_for_age(policy, age_days):
    """Return the injection form for a whole number of days of age."""
    previous = 0
    for tier in policy["tiers"]:
        max_age = tier["max_age_days"]
        if max_age is None or age_days <= max_age:
            return tier["form"]
        previous = max_age
    return policy["tiers"][-1]["form"]


def _day_of(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _age_days(day, today):
    """Calendar-day difference, clamped at 0. Uses date strings, not seconds,
    so a day boundary is exactly midnight (no partial-day overlap)."""
    from datetime import date
    year, month, day_num = (int(x) for x in day.split("-"))
    ty, tm, td = (int(x) for x in today.split("-"))
    return max(0, (date(ty, tm, td) - date(year, month, day_num)).days)


def compress(text, limit=1200):
    """Deterministic, model-free compression: trim to a character budget while
    keeping the head and tail, where operators usually care most."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - 20
    return text[:head] + "\n...[" + str(len(text) - head - tail) + " chars omitted]...\n" + text[-tail:]


def build_day(db, role_id, day):
    """Materialise (or refresh) one role-day row from its turns.

    Only completed turns contribute to conversational memory; a command turn is
    an administrative action, not a chat memory. An operator-pinned row
    (edited_at set) is left untouched.
    """
    row = db.execute("SELECT * FROM role_memory WHERE role_id=? AND day=?", (role_id, day)).fetchone()
    if row is not None and row["edited_at"] is not None:
        return row
    turns = db.execute(
        "SELECT * FROM role_turns WHERE role_id=? AND state='completed' ORDER BY seq",
        (role_id,)).fetchall()
    pieces, count = [], 0
    for turn in turns:
        if _day_of(turn["created_at"]) != day:
            continue
        if turn["source"] == "command":
            continue
        count += 1
        pieces.append("USER: " + (turn["prompt"] or ""))
        pieces.append("ASSISTANT: " + (turn["final_text"] or ""))
    full_text = "\n".join(pieces)
    now = time.time()
    db.execute(
        "INSERT INTO role_memory(role_id,day,full_text,compressed,summary,source,turn_count,generated_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(role_id,day) DO UPDATE SET "
        "full_text=excluded.full_text,compressed=excluded.compressed,summary=excluded.summary,"
        "source=excluded.source,turn_count=excluded.turn_count,generated_at=excluded.generated_at",
        (role_id, day, full_text, compress(full_text), _summarise(full_text), "derived", count, now))
    return db.execute("SELECT * FROM role_memory WHERE role_id=? AND day=?", (role_id, day)).fetchone()


def _summarise(text):
    """Placeholder coarse summary: first line + size marker.

    A model-authored summary can replace this later; it must never be generated
    synchronously on the context path, and the raw text stays available on
    demand, so a weak summary cannot lose information.
    """
    flat = " ".join(text.split())
    if not flat:
        return ""
    return compress(flat, 300)


def render_memory(db, role_id, today, policy=None):
    """Build the injectable memory block for a role, by age tier.

    Returns a list of {day, age_days, form, text} entries already ordered oldest
    first, plus the list of old days that are NOT injected and can be loaded on
    demand. Windows never overlap because the form is chosen per exact age.
    """
    policy = policy or policy_for(db, role_id)
    rows = db.execute("SELECT * FROM role_memory WHERE role_id=? ORDER BY day", (role_id,)).fetchall()
    injected, held_back = [], []
    for row in rows:
        age = _age_days(row["day"], today)
        form = form_for_age(policy, age)
        text = {"full": row["full_text"], "compressed": row["compressed"] or compress(row["full_text"]),
                "summary": row["summary"] or _summarise(row["full_text"])}[form]
        if not text:
            continue
        injected.append({"day": row["day"], "age_days": age, "form": form, "text": text})
        if form != "full":
            held_back.append(row["day"])
    return injected, held_back


def install_memory(app, transaction, audit, admin):
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel, ConfigDict, Field

    class Policy(BaseModel):
        model_config = ConfigDict(extra="forbid")
        tiers: list[dict] = Field(min_length=1, max_length=MAX_TIERS)

    class MemoryEdit(BaseModel):
        model_config = ConfigDict(extra="forbid")
        day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
        full_text: str = Field(max_length=200000)

    def require_role(db, role_id):
        if not db.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
            raise HTTPException(404, "Role not found")

    @app.put("/api/v1/roles/{role_id}/memory/policy", dependencies=[Depends(admin)])
    def set_policy(role_id: str, body: Policy):
        config = {"tiers": body.tiers}
        try:
            validate_policy(config)
        except PolicyError as error:
            raise HTTPException(422, str(error))
        with transaction() as db:
            require_role(db, role_id)
            db.execute("INSERT INTO role_memory_policy VALUES(?,?) ON CONFLICT(role_id) DO UPDATE SET config=excluded.config",
                       (role_id, json.dumps(config)))
            audit(db, "memory.policy_set", role_id, "admin", config)
        return config

    @app.get("/api/v1/roles/{role_id}/memory/policy", dependencies=[Depends(admin)])
    def get_policy(role_id: str):
        with transaction() as db:
            require_role(db, role_id)
            return policy_for(db, role_id)

    @app.get("/api/v1/roles/{role_id}/memory", dependencies=[Depends(admin)])
    def list_memory(role_id: str):
        with transaction() as db:
            require_role(db, role_id)
            return [dict(r) for r in db.execute("SELECT * FROM role_memory WHERE role_id=? ORDER BY day", (role_id,))]

    @app.get("/api/v1/roles/{role_id}/memory/{day}", dependencies=[Depends(admin)])
    def read_day(role_id: str, day: str):
        # On-demand loader for an old day's FULL text (roles are isolated).
        with transaction() as db:
            require_role(db, role_id)
            row = db.execute("SELECT * FROM role_memory WHERE role_id=? AND day=?", (role_id, day)).fetchone()
            if row is None:
                row = build_day(db, role_id, day)
            return dict(row)

    @app.put("/api/v1/roles/{role_id}/memory/{day}", dependencies=[Depends(admin)])
    def edit_day(role_id: str, day: str, body: MemoryEdit):
        with transaction() as db:
            require_role(db, role_id)
            now = time.time()
            db.execute(
                "INSERT INTO role_memory(role_id,day,full_text,compressed,summary,source,turn_count,edited_at,generated_at) "
                "VALUES(?,?,?,?,?,'edited',0,?,?) ON CONFLICT(role_id,day) DO UPDATE SET "
                "full_text=excluded.full_text,compressed=excluded.compressed,summary=excluded.summary,"
                "source='edited',edited_at=excluded.edited_at,generated_at=excluded.generated_at",
                (role_id, day, body.full_text, compress(body.full_text), _summarise(body.full_text), now, now))
            audit(db, "memory.edited", role_id, "admin", {"day": day})
            return dict(db.execute("SELECT * FROM role_memory WHERE role_id=? AND day=?", (role_id, day)).fetchone())

    @app.delete("/api/v1/roles/{role_id}/memory/{day}", dependencies=[Depends(admin)])
    def delete_day(role_id: str, day: str):
        with transaction() as db:
            require_role(db, role_id)
            if not db.execute("DELETE FROM role_memory WHERE role_id=? AND day=?", (role_id, day)).rowcount:
                raise HTTPException(404, "Memory day not found")
            audit(db, "memory.deleted", role_id, "admin", {"day": day})
        return {"deleted": True, "audit_retained": True}

    @app.post("/api/v1/roles/{role_id}/memory/rebuild", dependencies=[Depends(admin)])
    def rebuild(role_id: str):
        with transaction() as db:
            require_role(db, role_id)
            days = [r["day"] for r in db.execute(
                "SELECT DISTINCT date(created_at,'unixepoch','localtime') AS day FROM role_turns "
                "WHERE role_id=? AND state='completed' AND source!='command'", (role_id,)).fetchall() if r["day"]]
            for day in days:
                build_day(db, role_id, day)
            audit(db, "memory.rebuilt", role_id, "admin", {"days": days})
            return {"days": days}

    return {"render_memory": render_memory}

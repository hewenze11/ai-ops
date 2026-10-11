"""Server-side audit grouping; cursors pin the audit high-water mark."""
import base64
import json
from fastapi import Depends, HTTPException
from .scrubbing import redact_structure

CTE = """WITH evidence AS (
SELECT a.*, COALESCE(t.turn_id,r.id,e.turn_id,q.turn_id,ar.turn_id) turn_id,
 COALESCE(t.role_id,r.role_id,e.role_id,q.role_id,rr.role_id) role_id,
 t.id task_id,
 date(a.created_at,'unixepoch','+8 hours') day
FROM audit a LEFT JOIN tasks t ON t.id=a.entity_id
 LEFT JOIN role_turns r ON r.id=a.entity_id
 LEFT JOIN trigger_events e ON e.id=a.entity_id
 LEFT JOIN turn_questions q ON q.id=a.entity_id
 LEFT JOIN account_requests ar ON ar.id=a.entity_id
 LEFT JOIN role_turns rr ON rr.id=ar.turn_id
WHERE a.seq<=?
), grouped AS (SELECT *, COALESCE('turn:'||turn_id,'task:'||task_id,'day:'||day||':'||COALESCE(role_id,'service')) group_key FROM evidence)
"""

def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()

def decode(value):
    try:
        if len(value) > 1000:
            raise ValueError()
        return json.loads(base64.urlsafe_b64decode(value))
    except Exception:
        raise HTTPException(422, 'Invalid group cursor')

def install(app, admin):
    @app.get('/api/v1/logs/groups', dependencies=[Depends(admin)])
    def groups(role_id: str | None = None, day: str | None = None, before: str | None = None, limit: int = 30):
        if not 1 <= limit <= 100:
            raise HTTPException(422, 'Invalid limit')
        with app.state.read_transaction() as db:
            high = db.execute('SELECT COALESCE(MAX(seq),0) FROM audit').fetchone()[0]
            boundary = high + 1
            if before:
                cursor = decode(before)
                if not isinstance(cursor, list) or len(cursor) != 4 or cursor[2:] != [role_id, day] or type(cursor[0]) is not int or type(cursor[1]) is not int:
                    raise HTTPException(422, 'Cursor filters mismatch')
                high, boundary = cursor[:2]
            sql = CTE + """SELECT group_key,MAX(seq) last_seq,MIN(created_at) started_at,MAX(created_at) updated_at,
COUNT(*) event_count,MAX(turn_id) turn_id,MAX(task_id) task_id,MAX(role_id) role_id,MAX(day) day,
SUM(CASE WHEN event LIKE '%failed%' OR event LIKE '%rejected%' THEN 1 ELSE 0 END) error_events
FROM grouped WHERE (? IS NULL OR role_id=?) AND (? IS NULL OR day=?)
GROUP BY group_key HAVING MAX(seq)<? ORDER BY last_seq DESC LIMIT ?"""
            rows = [dict(r) for r in db.execute(sql, (high, role_id, role_id, day, day, boundary, limit + 1))]
            more = len(rows) > limit
            rows = rows[:limit]
            for row in rows:
                row['id'] = encode([row.pop('group_key'), high, day])
                if row['turn_id']:
                    turn = db.execute('SELECT prompt,state FROM role_turns WHERE id=?', (row['turn_id'],)).fetchone()
                    row['summary'] = turn['prompt'][:200] if turn else 'Archived turn'
                    row['state'] = turn['state'] if turn else None
                elif row['task_id']:
                    row['summary'] = '任务 · ' + row['task_id']
                else:
                    row['summary'] = '平台事件（按日聚合）'
            return {'items': redact_structure(rows), 'next_before': encode([high, rows[-1]['last_seq'], role_id, day]) if more else None, 'timezone': 'Asia/Shanghai', 'snapshot_seq': high}

    @app.get('/api/v1/logs/groups/{group_id}/events', dependencies=[Depends(admin)])
    def events(group_id: str, after: int = 0, limit: int = 100):
        value = decode(group_id)
        if not isinstance(value, list) or len(value) not in (2, 3) or not isinstance(value[0], str) or type(value[1]) is not int or value[1] < 0 or not 1 <= limit <= 200 or after < 0:
            raise HTTPException(422, 'Invalid group pagination')
        day = value[2] if len(value) == 3 else None
        if day is not None and not isinstance(day, str):
            raise HTTPException(422, 'Invalid group date')
        with app.state.read_transaction() as db:
            rows = [dict(r) for r in db.execute(CTE + 'SELECT * FROM grouped WHERE group_key=? AND seq>? AND (? IS NULL OR day=?) ORDER BY seq LIMIT ?', (value[1], value[0], after, day, day, limit + 1))]
            more = len(rows) > limit
            rows = rows[:limit]
            for row in rows:
                row['details'] = json.loads(row['details'])
            return {'items': redact_structure(rows), 'next_after': rows[-1]['seq'] if more else None}

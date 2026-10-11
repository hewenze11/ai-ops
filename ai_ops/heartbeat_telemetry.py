"""Optional protocol-1.2 attempt evidence. Receipt and sender ACK are independent."""
from .scrubbing import redact_text
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi import HTTPException

MAX_SEQ = 9007199254740991  # exact in JS and SQLite; never iterate a sequence gap
RETENTION = 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeat_streams(
 id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL REFERENCES assets(id),
 session_id TEXT NOT NULL, controller TEXT NOT NULL, high_seq INTEGER NOT NULL,
 updated_at REAL NOT NULL, retained_from_seq INTEGER NOT NULL DEFAULT 1,
 UNIQUE(asset_id,session_id,controller));
CREATE TABLE IF NOT EXISTS heartbeat_attempts(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, stream_id INTEGER NOT NULL REFERENCES heartbeat_streams(id),
 attempt_seq INTEGER NOT NULL, received_at REAL, ack_status TEXT NOT NULL DEFAULT 'unknown',
 recorded_at REAL NOT NULL, UNIQUE(stream_id,attempt_seq));
CREATE INDEX IF NOT EXISTS heartbeat_attempt_time ON heartbeat_attempts(recorded_at);
"""

class Result(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempt_seq: int = Field(strict=True, ge=1, le=MAX_SEQ)
    outcome: Literal['acknowledged', 'unconfirmed']

class Telemetry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    controller: str = Field(min_length=1, max_length=64)
    attempt_seq: int = Field(strict=True, ge=1, le=MAX_SEQ)
    previous_results: list[Result] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def valid_results(self):
        seqs = [r.attempt_seq for r in self.previous_results]
        if len(set(seqs)) != len(seqs) or any(not max(1, self.attempt_seq - 10) <= s < self.attempt_seq for s in seqs):
            raise ValueError('Results must be unique previous ten attempt sequences')
        if any(ord(c) < 32 for c in self.controller):
            raise ValueError('Invalid controller identity')
        return self


def prune(db, now):
    cutoff = now - RETENTION
    # Remember pruned evidence; a retained sequence window must not call it lost.
    expired = db.execute('SELECT stream_id,MAX(attempt_seq)+1 AS floor FROM heartbeat_attempts WHERE recorded_at<? GROUP BY stream_id', (cutoff,)).fetchall()
    db.executemany('UPDATE heartbeat_streams SET retained_from_seq=MAX(retained_from_seq,?) WHERE id=?', [(r['floor'],r['stream_id']) for r in expired])
    db.execute('DELETE FROM heartbeat_attempts WHERE recorded_at<?', (cutoff,))
    db.execute('DELETE FROM heartbeat_attempts WHERE stream_id IN (SELECT id FROM heartbeat_streams WHERE updated_at<?)', (cutoff,))
    db.execute('DELETE FROM heartbeat_streams WHERE updated_at<?', (cutoff,))


def record(db, asset_id, telemetry, now):
    prune(db, now)
    t = telemetry
    row = db.execute('SELECT * FROM heartbeat_streams WHERE asset_id=? AND session_id=? AND controller=?', (asset_id,t.session_id,t.controller)).fetchone()
    if row is None:
        if db.execute('SELECT COUNT(*) FROM heartbeat_streams WHERE asset_id=?', (asset_id,)).fetchone()[0] >= 32:
            raise HTTPException(429, 'Heartbeat stream limit; retry after telemetry retention expires')
        db.execute('INSERT INTO heartbeat_streams(asset_id,session_id,controller,high_seq,updated_at) VALUES(?,?,?,?,?)', (asset_id,t.session_id,t.controller,t.attempt_seq,now))
        row = db.execute('SELECT * FROM heartbeat_streams WHERE id=last_insert_rowid()').fetchone()
    sid = row['id']
    high = max(row['high_seq'], t.attempt_seq)
    # At most 50k sequence positions per stream, including sender reports. Ancient
    # out-of-order packets are acknowledged, but cannot expand retained history.
    floor = max(row['retained_from_seq'], high - 49999)
    db.execute('UPDATE heartbeat_streams SET high_seq=?,updated_at=?,retained_from_seq=? WHERE id=?', (high,now,floor,sid))
    db.execute('DELETE FROM heartbeat_attempts WHERE stream_id=? AND attempt_seq<?', (sid,floor))
    if t.attempt_seq >= floor:
        db.execute('''INSERT INTO heartbeat_attempts(stream_id,attempt_seq,received_at,recorded_at) VALUES(?,?,?,?)
            ON CONFLICT(stream_id,attempt_seq) DO UPDATE SET
            recorded_at=CASE WHEN heartbeat_attempts.received_at IS NULL THEN excluded.recorded_at ELSE heartbeat_attempts.recorded_at END,
            received_at=COALESCE(heartbeat_attempts.received_at,excluded.received_at)''', (sid,t.attempt_seq,now,now))
    for result in t.previous_results:
        if result.attempt_seq >= floor:
            db.execute('''INSERT INTO heartbeat_attempts(stream_id,attempt_seq,ack_status,recorded_at) VALUES(?,?,?,?)
                ON CONFLICT(stream_id,attempt_seq) DO UPDATE SET ack_status=CASE
                WHEN heartbeat_attempts.ack_status='acknowledged' THEN 'acknowledged' ELSE excluded.ack_status END''', (sid,result.attempt_seq,result.outcome,now))
    current_id = db.execute('SELECT MAX(id) FROM heartbeat_streams WHERE asset_id=? AND controller=?', (asset_id,t.controller)).fetchone()[0]
    return sid == current_id and t.attempt_seq >= row['high_seq']


def view(db, asset_id, now):
    # Last-created session for each controller; a delayed known old session never
    # replaces a newer session. Names are Agent assertions, not authorization.
    streams = db.execute('''SELECT * FROM heartbeat_streams WHERE asset_id=? AND updated_at>=?
        AND id IN (SELECT MAX(id) FROM heartbeat_streams WHERE asset_id=? GROUP BY controller) ORDER BY id DESC''', (asset_id,now-RETENTION,asset_id)).fetchall()
    output = []
    for stream in streams:
        hi = stream['high_seq']
        lo = max(1, hi-9)
        rows = {r['attempt_seq']: r for r in db.execute('SELECT * FROM heartbeat_attempts WHERE stream_id=? AND attempt_seq BETWEEN ? AND ? AND recorded_at>=?', (stream['id'],lo,hi,now-RETENTION))}
        attempts = []
        for seq in range(lo,hi+1):
            r = rows.get(seq)
            receipt = 'received' if r and r['received_at'] is not None else ('unknown_expired' if seq < stream['retained_from_seq'] else 'not_received_yet')
            attempts.append({'attempt_seq':seq,'receipt':receipt,'received_at':r['received_at'] if r else None,'ack_status':r['ack_status'] if r else 'unknown'})
        received = sum(a['receipt']=='received' for a in attempts)
        output.append({'controller':redact_text(stream['controller']),'session_id':stream['session_id'],
            'from_seq':lo,'through_seq':hi,'attempt_count':len(attempts),'received_count':received,
            'not_received_count':sum(a['receipt']=='not_received_yet' for a in attempts),
            'unknown_count':sum(a['receipt']=='unknown_expired' for a in attempts),
            'acknowledged_count':sum(a['ack_status']=='acknowledged' for a in attempts),
            'unconfirmed_count':sum(a['ack_status']=='unconfirmed' for a in attempts),
            'updated_at':stream['updated_at'],'stale':now-stream['updated_at']>30,'attempts':attempts})
    if not output:
        return None
    return {'metric':'heartbeat_attempt_receipt','actual_attempts':'last_observable_sequence_window',
        'metric_label':'最后可观测发送窗口','tail_attempts':'unknown','window_size':10,
        'streams':output, **output[0]}


def logs(db, asset_id, after, limit, now):
    rows = [dict(r) for r in db.execute('''SELECT a.seq,s.asset_id,s.session_id,s.controller,a.attempt_seq,
        a.received_at,a.ack_status,a.recorded_at FROM heartbeat_attempts a JOIN heartbeat_streams s ON s.id=a.stream_id
        WHERE s.asset_id=? AND a.seq>? AND a.recorded_at>=? ORDER BY a.seq LIMIT ?''', (asset_id,after,now-RETENTION,limit))]
    for row in rows:
        row['controller'] = redact_text(row['controller'])
    return rows

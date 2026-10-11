"""Durable single-choice decisions. Answers are data, never execution approval."""
import json
import time
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

OptionID = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]

SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_questions(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES role_turns(id),
 role_id TEXT NOT NULL REFERENCES roles(id), tool_call_id TEXT NOT NULL,
 question TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 answer TEXT, created_at REAL NOT NULL, answered_at REAL,
 UNIQUE(turn_id,tool_call_id));
CREATE INDEX IF NOT EXISTS questions_by_turn ON turn_questions(turn_id,created_at);
"""


class Option(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: OptionID
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=500)


class AskUser(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=2000)
    options: list[Option] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids) or any(i.lower() == 'other' for i in ids):
            raise ValueError('Option IDs must be unique and must not use reserved other')
        return self


class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    option_id: OptionID
    text: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def other_text(self):
        if self.option_id == 'other' and not self.text:
            raise ValueError('其他选项必须填写内容')
        if self.option_id != 'other' and self.text:
            raise ValueError('Only other accepts custom text')
        return self


def tool_spec():
    schema = AskUser.model_json_schema()
    # Inline the one nested schema for broad OpenAI-compatible support.
    schema['properties']['options']['items'] = schema.pop('$defs')['Option']
    return {'type': 'function', 'function': {
        'name': 'ask_user',
        'description': 'Ask a necessary business decision or missing critical information, then pause this turn until the human answers in the Web chat. One single-choice question, 1-8 options with unique stable IDs. Platform always adds other with custom text; do not supply an other option. This is NOT execution approval and cannot change authority. Continue yourself when no critical decision is needed.',
        'parameters': schema}}


def create_question(db, turn, call, arguments):
    question = AskUser.model_validate(arguments)
    tool_id = call.get('id')
    if not isinstance(tool_id, str) or not 1 <= len(tool_id) <= 200:
        raise ValueError('Invalid tool call ID')
    qid = str(uuid.uuid4())
    db.execute('INSERT INTO turn_questions(id,turn_id,role_id,tool_call_id,question,options,created_at) VALUES(?,?,?,?,?,?,?)',
               (qid, turn['id'], turn['role_id'], tool_id, question.question,
                json.dumps([o.model_dump() for o in question.options], ensure_ascii=False), time.time()))
    return qid


def questions_for_turn(db, turn_id):
    return [{**dict(r), 'options': json.loads(r['options']),
             'answer': json.loads(r['answer']) if r['answer'] else None}
            for r in db.execute('SELECT * FROM turn_questions WHERE turn_id=? ORDER BY created_at,id', (turn_id,))]


def install_questions(app, transaction, audit, admin):
    from .turns import set_turn_state, TERMINAL

    @app.post('/api/v1/turns/{turn_id}/questions/{question_id}/answer', dependencies=[Depends(admin)])
    def answer_question(turn_id: str, question_id: str, body: Answer):
        # BEGIN IMMEDIATE is owned by transaction: concurrent answers and
        # cancellation serialize, including transcript append and turn resume.
        with transaction() as db:
            turn = db.execute('SELECT * FROM role_turns WHERE id=?', (turn_id,)).fetchone()
            q = db.execute('SELECT * FROM turn_questions WHERE id=? AND turn_id=?', (question_id, turn_id)).fetchone()
            if not turn or not q or q['role_id'] != turn['role_id']:
                raise HTTPException(404, 'Question not found for this turn')
            options = json.loads(q['options'])
            option = next((o for o in options if o['id'] == body.option_id), None)
            if body.option_id != 'other' and option is None:
                raise HTTPException(422, 'Invalid option ID')
            result = {'source': 'user_input', 'question_id': question_id,
                      'option_id': body.option_id, 'label': option['label'] if option else '其他',
                      'text': body.text, 'grants_execution_approval': False}
            if q['state'] == 'answered':
                if json.loads(q['answer']) != result:
                    raise HTTPException(409, 'Question already answered differently')
                # Idempotent acknowledgement only; never wakes a terminal turn.
                return {'question_id': question_id, 'state': turn['state'], 'duplicate': True}
            if q['state'] != 'pending' or turn['state'] != 'waiting_user' or turn['state'] in TERMINAL:
                raise HTTPException(409, 'Question is no longer awaiting an answer')
            messages = json.loads(turn['messages'])
            if not messages or not any(c.get('id') == q['tool_call_id'] and c.get('function', {}).get('name') == 'ask_user' for c in messages[-1].get('tool_calls', [])):
                raise HTTPException(409, 'Question does not match the pending tool call')
            content = json.dumps(result, ensure_ascii=False)
            messages.append({'role': 'tool', 'tool_call_id': q['tool_call_id'], 'content': content})
            db.execute("UPDATE turn_questions SET state='answered',answer=?,answered_at=? WHERE id=?", (content, time.time(), question_id))
            db.execute('UPDATE role_turns SET messages=? WHERE id=?', (json.dumps(messages, ensure_ascii=False), turn_id))
            set_turn_state(db, turn_id, 'ready')
            audit(db, 'question.answered', question_id, 'admin', {'turn_id': turn_id, 'option_id': body.option_id})
            return {'question_id': question_id, 'state': 'ready', 'duplicate': False}

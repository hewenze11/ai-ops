"""Local text import preview. No archive extraction, code execution, or network."""
import json
import re
from pathlib import PurePosixPath
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=200000)

def parse(filename, content):
    name = PurePosixPath(filename.replace('\\', '/')).name
    suffix = name.lower().rsplit('.', 1)[-1]
    if suffix == 'json':
        try:
            raw = json.loads(content)
            entries = raw.get('skills', [raw]) if isinstance(raw, dict) else raw
            if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
                raise ValueError()
            result = []
            for item in entries:
                if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('content'), str) or not 1 <= len(item['name']) <= 200 or len(item['content']) > 200000:
                    raise ValueError()
                # Deliberately discard ids/bindings/enabled/permissions from input.
                result.append({'name': item['name'], 'content': item['content'], 'description': ''})
            return result
        except (ValueError, TypeError):
            raise HTTPException(422, 'Invalid platform Skill JSON or bundle')
    if suffix != 'md':
        raise HTTPException(422, 'Only .md / SKILL.md and platform JSON text are supported; ZIP/scripts are not installed')
    metadata = {}
    body = content.lstrip('\ufeff').replace('\r\n', '\n')
    if body.startswith('---\n'):
        end = body.find('\n---', 4)
        if end < 0:
            raise HTTPException(422, 'Unterminated frontmatter')
        header = body[4:end]
        # Conservative scalar subset; preserve full original text regardless.
        lines = header.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r'^(name|description):\s*(.*)$', line)
            if match:
                value = match[2].strip()
                if value in ('>', '|', '>-', '|-'):
                    continuation = []
                    for following in lines[index + 1:]:
                        if following and not following[0].isspace():
                            break
                        continuation.append(following.strip())
                    value = ' '.join(continuation)
                elif len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                metadata[match[1]] = value
    title = metadata.get('name') or re.sub(r'\.md$', '', name, flags=re.I)
    if not 1 <= len(title) <= 200:
        raise HTTPException(422, 'Skill name must be 1-200 characters')
    return [{'name': title, 'description': metadata.get('description', '')[:2000], 'content': content}]

def install(app, admin):
    @app.post('/api/v1/skills/import-preview', dependencies=[Depends(admin)])
    def preview(body: Preview):
        return {'skills': parse(body.filename, body.content), 'writes': False, 'scripts_installed': False, 'requires_role_confirmation': True}

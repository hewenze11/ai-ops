"""Role-scoped image input. Files are private, immutable and referenced by IDs.

No automatic expiry: limits prevent unbounded upload. Originals are normalized to
strip metadata; thumbnails are separate. Neither image bytes nor paths enter audit.
"""
import base64
from contextlib import closing
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
import warnings

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_PER_TURN = 4
SCHEMA = '''
CREATE TABLE IF NOT EXISTS image_attachments(
 id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES roles(id),
 digest TEXT NOT NULL, thumb_digest TEXT NOT NULL, mime TEXT NOT NULL,
 byte_size INTEGER NOT NULL, thumb_size INTEGER NOT NULL,
 width INTEGER NOT NULL, height INTEGER NOT NULL, created_at REAL NOT NULL,
 UNIQUE(role_id,digest));
CREATE TABLE IF NOT EXISTS turn_attachments(
 turn_id TEXT NOT NULL REFERENCES role_turns(id), attachment_id TEXT NOT NULL REFERENCES image_attachments(id),
 position INTEGER NOT NULL, PRIMARY KEY(turn_id,attachment_id), UNIQUE(turn_id,position));
'''


def root_for(db_path):
    return Path(os.environ.get('AI_OPS_ATTACHMENT_DIR') or Path(db_path).parent / 'attachments')


def file_path(root, ident, thumbnail=False):
    if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
        raise ValueError('Invalid attachment ID')
    path = Path(root) / (ident + ('.thumb.png' if thumbnail else '.png'))
    if path.is_symlink():
        raise ValueError('Attachment symlinks are not permitted')
    return path


def normalize(raw):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('Unsupported image format')
                if original.width * original.height > MAX_PIXELS:
                    raise ValueError('Image exceeds 16 million pixels')
                if getattr(original, 'n_frames', 1) != 1:
                    raise ValueError('Animated images are not supported')
                original.load()
                image = ImageOps.exif_transpose(original).convert('RGBA' if 'A' in original.getbands() else 'RGB')
                # Lossless PNG preserves terminal/error text and excludes EXIF.
                image.info.clear()
                output = io.BytesIO()
                image.save(output, format='PNG')
                data = output.getvalue()
                if len(data) > MAX_UPLOAD:
                    raise ValueError('Normalized image exceeds 8 MiB')
                size = image.size
                image.thumbnail((384, 384))
                thumb = io.BytesIO()
                image.save(thumb, format='PNG')
                return data, thumb.getvalue(), size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, 'Invalid image: only non-animated JPEG/PNG/WebP, at most 16MP and 8MiB normalized') from None


def view(row):
    return {k: row[k] for k in ('id', 'role_id', 'mime', 'byte_size', 'width', 'height', 'created_at')}


def validate_ids(db, role_id, ids):
    if len(ids) > MAX_PER_TURN or len(ids) != len(set(ids)):
        raise HTTPException(422, 'At most four distinct images per turn')
    for ident in ids:
        row = db.execute('SELECT role_id FROM image_attachments WHERE id=?', (ident,)).fetchone()
        if row is None or row['role_id'] != role_id:
            raise HTTPException(403, 'Image is not available to this role')


def bind(db, turn_id, role_id, ids):
    validate_ids(db, role_id, ids)
    db.executemany('INSERT INTO turn_attachments VALUES(?,?,?)', [(turn_id, ident, i) for i, ident in enumerate(ids)])


def for_turn(db, turn_id):
    return [view(row) for row in db.execute('SELECT a.* FROM image_attachments a JOIN turn_attachments t ON t.attachment_id=a.id WHERE t.turn_id=? ORDER BY t.position', (turn_id,))]


def content_parts(db, root, turn, text):
    rows = for_turn(db, turn['id'])
    if not rows:
        return text
    parts = [{'type': 'text', 'text': text}]
    for row in rows:
        try:
            data = file_path(root, row['id']).read_bytes()
        except (OSError, ValueError):
            from .model_client import ModelFailure
            raise ModelFailure('IMAGE_ATTACHMENT_UNAVAILABLE') from None
        parts.append({'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii'), 'detail': 'auto'}})
    return parts


def install(app, db_path, admin):
    root = root_for(db_path)
    app.state.attachment_root = root

    @app.post('/api/v1/attachments', dependencies=[Depends(admin)], status_code=201)
    async def upload(request: Request, role_id: str):
        from .role_lifecycle import require_active
        mime = request.headers.get('content-type', '').split(';')[0].lower()
        if mime not in ('image/png', 'image/jpeg', 'image/webp'):
            raise HTTPException(415, 'Send image bytes with image/png, image/jpeg or image/webp Content-Type')
        with app.state.read_transaction() as db:
            require_active(db, role_id)
        raw = bytearray()
        async for block in request.stream():
            raw.extend(block)
            if len(raw) > MAX_UPLOAD:
                raise HTTPException(413, 'Image upload exceeds 8 MiB')
        # CPU decoding occurs outside the database lock and ASGI event loop.
        from starlette.concurrency import run_in_threadpool
        data, thumb, size = await run_in_threadpool(normalize, bytes(raw))
        digest = hashlib.sha256(data).hexdigest()
        written = []
        try:
            with app.state.transaction() as db:
                require_active(db, role_id)
                old = db.execute('SELECT * FROM image_attachments WHERE role_id=? AND digest=?', (role_id, digest)).fetchone()
                if old:
                    return {**view(old), 'duplicate': True}
                used = db.execute('SELECT COALESCE(SUM(byte_size+thumb_size),0) FROM image_attachments').fetchone()[0]
                quota = int(os.environ.get('AI_OPS_ATTACHMENT_QUOTA_BYTES', str(256 * 1024 * 1024)))
                if used + len(data) + len(thumb) > quota:
                    raise HTTPException(413, 'Attachment storage quota reached; no existing evidence was deleted')
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(root, 0o700)
                ident = uuid.uuid4().hex
                for is_thumb, content in ((False, data), (True, thumb)):
                    path = file_path(root, ident, is_thumb)
                    with path.open('xb') as output:
                        written.append(path)
                        os.chmod(path, 0o600)
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                db.execute('INSERT INTO image_attachments VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (ident, role_id, digest, hashlib.sha256(thumb).hexdigest(), 'image/png', len(data), len(thumb), *size, time.time()))
                app.state.audit(db, 'attachment.created', ident, 'admin-token-domain', {'role_id': role_id, 'byte_size': len(data), 'width': size[0], 'height': size[1]})
                row = db.execute('SELECT * FROM image_attachments WHERE id=?', (ident,)).fetchone()
                result = {**view(row), 'duplicate': False}
            return result
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise

    @app.get('/api/v1/attachments/{attachment_id}', dependencies=[Depends(admin)])
    def download(attachment_id: str, role_id: str, thumbnail: bool = False):
        with app.state.read_transaction() as db:
            validate_ids(db, role_id, [attachment_id])
        try:
            path = file_path(root, attachment_id, thumbnail)
        except ValueError:
            raise HTTPException(404, 'Attachment unavailable') from None
        if not path.is_file():
            raise HTTPException(404, 'Attachment unavailable')
        return FileResponse(path, media_type='image/png', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline'})


def backup_files(db_path):
    """Returns immutable file names and expected hashes; handles old snapshots."""
    with closing(sqlite3.connect('file:' + Path(db_path).as_posix() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='image_attachments'").fetchone():
            return {}
        files = {}
        for ident, digest, thumb_digest in db.execute('SELECT id,digest,thumb_digest FROM image_attachments'):
            files[file_path('.', ident).name] = digest
            files[file_path('.', ident, True).name] = thumb_digest
        return files


def verify_files(files, folder):
    for name, digest in files.items():
        p = Path(folder) / name
        if p.is_symlink() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError('Attachment backup missing or corrupted')


def copy_files(files, source, dest):
    if not files:
        return
    verify_files(files, source)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dest, 0o700)
    for name, digest in files.items():
        target = dest / name
        if target.exists():
            verify_files({name: digest}, dest)
            continue
        temporary = dest / (name + '.' + uuid.uuid4().hex + '.partial')
        try:
            with temporary.open('xb') as output, (Path(source) / name).open('rb') as src:
                os.chmod(temporary, 0o600)
                shutil.copyfileobj(src, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def compact_request(body):
    """Keep text/tool evidence but never duplicate base64 images in model_calls.

    Actual attachments are resolved from turn_attachments in order. This also
    measures the text context limit without counting binary transport encoding.
    """
    messages = []
    for message in body.get('messages', []):
        content = message.get('content')
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get('type') == 'image_url':
                    parts.append({'type': 'image_reference', 'note': 'See this turn attachments; binary payload omitted'})
                else:
                    parts.append(part)
            messages.append({**message, 'content': parts})
        else:
            messages.append(message)
    return {**body, 'messages': messages}

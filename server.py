#!/usr/bin/env python3
"""Servidor do Avenida Livre: entrega o jogo e o multiplayer por WebSocket.

Só usa a biblioteca padrão do Python 3.8+. Rode com:  python server.py  [porta]
A variável de ambiente PORT, se existir, tem prioridade (é o que Render/Railway usam). Padrão: 8080.
"""
import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import struct
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 8080))
HOST = os.environ.get('HOST', '0.0.0.0')
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

MAPS = {'paulista', 'copacabana', 'esplanada'}
TEAMS = {'va', 'vm', 'pa'}
VEHICLES = {'pe', 'carro', 'roadster', 'carreta', 'dirigivel', 'moto', 'chopper', 'girafa', 'dino',
            'calango', 'onca', 'pipoca', 'somcar', 'som'}
ANIMS = {'idle', 'walk', 'run', 'push', 'pedal', 'dance', 'wave', 'clap', 'sit', 'sitground', 'ride',
         'stand', 'jump', 'spray', 'ko'}
STAMPS = {'tag', 'coracao', 'estrela', 'paz', 'time', 'smile'}
BAD = ['idiota', 'burro', 'lixo', 'otario', 'otário', 'imbecil', 'merda', 'porra', 'caralho', 'fdp',
       'vagabundo', 'vagabunda']
STATIC_EXT = {'.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.webp', '.svg', '.ico', '.json', '.woff2', '.mp3', '.ogg'}
MAX_FRAME = 64 * 1024
MAX_PER_ROOM = 300
TICK = 0.1

conns = set()
_next_id = 1


def clean_text(s, n):
    s = re.sub(r'[\x00-\x1f\x7f]', ' ', str(s or '')).strip()[:n]
    for w in BAD:
        s = re.sub(re.escape(w), '*' * len(w), s, flags=re.I)
    return s


def num(v, lo, hi, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def clean_look(look):
    out = {}
    if isinstance(look, dict):
        for k, v in list(look.items())[:12]:
            if isinstance(k, str) and len(k) <= 12 and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 24:
                out[k] = v
    return out


def clean_held(h):
    if not isinstance(h, dict):
        return None
    t = h.get('type')
    if t in ('flag', 'obj'):
        return {'type': t, 'id': clean_text(h.get('id'), 16), 'text': clean_text(h.get('text'), 30)}
    if t == 'cartaz':
        color = lambda c: c if isinstance(c, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', c) else '#ffffff'
        return {'type': 'cartaz', 'text': clean_text(h.get('text'), 42) or 'PAZ', 'bg': color(h.get('bg')), 'fg': color(h.get('fg'))}
    return None


class Conn:
    def __init__(self, reader, writer):
        global _next_id
        self.r, self.w = reader, writer
        self.id = _next_id
        _next_id += 1
        self.room = 'paulista'
        self.p = None              # dados do personagem quando está jogando (None = só assistindo)
        self.q = asyncio.Queue(maxsize=400)
        self.alive = True
        self.chat_t = 0.0
        self.chat_tokens = 4.0
        self.spray_t = 0.0
        self.hit_t = 0.0

    def send(self, obj):
        if not self.alive:
            return
        try:
            self.q.put_nowait(json.dumps(obj, separators=(',', ':'), ensure_ascii=False))
        except asyncio.QueueFull:
            pass  # cliente lento: descarta em vez de travar o servidor


def frame(data: bytes, opcode=1):
    n = len(data)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack('!H', n)
    else:
        head += bytes([127]) + struct.pack('!Q', n)
    return head + data


async def read_frame(r):
    b1, b2 = await r.readexactly(2)
    op, masked, ln = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
    if ln == 126:
        ln = struct.unpack('!H', await r.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack('!Q', await r.readexactly(8))[0]
    if ln > MAX_FRAME:
        raise ValueError('frame grande demais')
    mask = await r.readexactly(4) if masked else b''
    data = bytearray(await r.readexactly(ln))
    if masked:
        for i in range(ln):
            data[i] ^= mask[i & 3]
    return b1 & 0x80, op, bytes(data)


def in_room(room, playing_only=False):
    return [c for c in conns if c.room == room and (c.p or not playing_only)]


def broadcast(room, obj, exclude=None, team=None):
    for c in in_room(room):
        if c is exclude:
            continue
        if team and (not c.p or c.p['team'] != team):
            continue
        c.send(obj)


def public(c):
    p = c.p
    return {'id': c.id, 'nick': p['nick'], 'team': p['team'], 'look': p['look'], 'held': p['held'], 'prem': p['prem'],
            'x': p['x'], 'y': p['y'], 'z': p['z'], 'h': p['h']}


def roster(room):
    return [public(c) for c in in_room(room, True)]


def counts():
    by_team = {t: 0 for t in TEAMS}
    by_map = {m: 0 for m in MAPS}
    for c in conns:
        if c.p:
            by_team[c.p['team']] += 1
            by_map[c.room] += 1
    return {'t': 'counts', 'c': by_team, 'maps': by_map, 'total': sum(by_team.values()), 'watch': len(conns)}


def apply_profile(c, m):
    old = c.p or {}
    c.p = {
        'nick': clean_text(m.get('nick'), 16) or 'Anônimo',
        'team': m.get('team') if m.get('team') in TEAMS else 'va',
        'look': clean_look(m.get('look')),
        'held': clean_held(m.get('held')),
        'prem': bool(m.get('prem')),
        'x': old.get('x', num(m.get('x'), -500, 500)), 'y': old.get('y', 0.0), 'z': old.get('z', num(m.get('z'), -500, 500)),
        'h': old.get('h', num(m.get('h'), -1e4, 1e4)),
        'a': old.get('a', 'idle'), 'v': old.get('v', 'pe'), 'pvp': old.get('pvp', 0), 'atk': 0,
    }
    if len(c.p['nick']) < 2:
        c.p['nick'] = 'Anônimo'


def change_room(c, room):
    if room not in MAPS or room == c.room:
        if room in MAPS:
            c.send({'t': 'roster', 'ps': roster(room)})
        return
    if c.p:
        broadcast(c.room, {'t': 'leave', 'id': c.id}, exclude=c)
    c.room = room
    c.send({'t': 'roster', 'ps': roster(room)})
    if c.p:
        broadcast(room, {'t': 'join', 'p': public(c)}, exclude=c)


def handle(c, m):
    t = m.get('t')
    now = time.monotonic()
    if t == 'hello':
        room = m.get('map') if m.get('map') in MAPS else c.room
        if len(in_room(room, True)) >= MAX_PER_ROOM and not c.p:
            c.send({'t': 'full'})
            return
        was = c.p is not None
        apply_profile(c, m)
        if room != c.room:
            if was:
                broadcast(c.room, {'t': 'leave', 'id': c.id}, exclude=c)
            c.room = room
        c.send({'t': 'roster', 'ps': roster(room)})
        broadcast(room, {'t': 'join', 'p': public(c)}, exclude=c)
    elif t == 'look' and c.p:
        apply_profile(c, m)
        broadcast(c.room, {'t': 'look', 'p': public(c)}, exclude=c)
    elif t == 'leave' and c.p:
        broadcast(c.room, {'t': 'leave', 'id': c.id}, exclude=c)
        c.p = None
    elif t == 'map':
        change_room(c, m.get('map'))
    elif t == 'st' and c.p:
        p = c.p
        p['x'], p['y'], p['z'] = num(m.get('x'), -500, 500), num(m.get('y'), -5, 120), num(m.get('z'), -500, 500)
        p['h'] = num(m.get('h'), -1e4, 1e4)
        p['a'] = m.get('a') if m.get('a') in ANIMS else 'idle'
        p['v'] = m.get('v') if m.get('v') in VEHICLES else 'pe'
        p['pvp'] = 1 if m.get('p') else 0
        p['atk'] = 1 if m.get('atk') else 0
    elif t == 'chat' and c.p:
        c.chat_tokens = min(4.0, c.chat_tokens + (now - c.chat_t) * 0.8)
        c.chat_t = now
        if c.chat_tokens < 1:
            c.send({'t': 'sys', 'text': 'Calma! Você está mandando mensagens rápido demais.'})
            return
        c.chat_tokens -= 1
        text = clean_text(m.get('text'), 120)
        if not text:
            return
        team_only = bool(m.get('teamOnly'))
        msg = {'t': 'chat', 'id': c.id, 'nick': c.p['nick'], 'team': c.p['team'], 'text': text, 'teamOnly': team_only, 'prem': c.p['prem']}
        broadcast(c.room, msg, exclude=c, team=c.p['team'] if team_only else None)
    elif t == 'spray' and c.p:
        if now - c.spray_t < 0.4:
            return
        c.spray_t = now
        color = m.get('color') if isinstance(m.get('color'), str) and re.fullmatch(r'#[0-9a-fA-F]{6}', m.get('color')) else '#ff3b6b'
        broadcast(c.room, {'t': 'spray', 'x': num(m.get('x'), -500, 500), 'z': num(m.get('z'), -500, 500),
                           'stamp': m.get('stamp') if m.get('stamp') in STAMPS else 'tag', 'color': color,
                           'nick': c.p['nick'], 'team': c.p['team']}, exclude=c)
    elif t == 'super' and c.p:
        amount = m.get('amount') if m.get('amount') in (2, 5, 10, 20) else 2
        text = clean_text(m.get('text'), 90)
        if text:
            broadcast(c.room, {'t': 'super', 'id': c.id, 'nick': c.p['nick'], 'team': c.p['team'], 'text': text,
                               'amount': amount, 'prem': c.p['prem']}, exclude=c)
    elif t == 'hit' and c.p:
        if now - c.hit_t < 0.3:
            return
        c.hit_t = now
        target = next((o for o in conns if o.id == m.get('id') and o.p and o.room == c.room), None)
        if not target or not c.p['pvp'] or not target.p['pvp'] or target.p['team'] == c.p['team']:
            return
        if (target.p['x'] - c.p['x']) ** 2 + (target.p['z'] - c.p['z']) ** 2 > 3.5 ** 2:
            return
        target.send({'t': 'dmg', 'from': c.id, 'nick': c.p['nick'], 'amount': random.randint(22, 32)})
    elif t == 'report':
        line = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'by': c.p['nick'] if c.p else None,
                'target': clean_text(m.get('nick'), 16), 'text': clean_text(m.get('text'), 160)}
        with open(os.path.join(ROOT, 'denuncias.log'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')


async def writer_loop(c):
    try:
        while c.alive:
            data = await c.q.get()
            c.w.write(frame(data.encode('utf-8')))
            await c.w.drain()
    except (ConnectionError, OSError):
        c.alive = False


async def ws_session(reader, writer, headers):
    key = headers.get('sec-websocket-key', '')
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  f'Sec-WebSocket-Accept: {accept}\r\n\r\n').encode())
    await writer.drain()
    c = Conn(reader, writer)
    conns.add(c)
    wtask = asyncio.create_task(writer_loop(c))
    c.send({'t': 'welcome', 'id': c.id})
    c.send(counts())
    try:
        buf = b''
        while True:
            fin, op, data = await read_frame(reader)
            if op == 8:
                break
            if op == 9:
                writer.write(frame(data, 10))
                continue
            if op in (1, 0):
                buf += data
                if len(buf) > MAX_FRAME:
                    break
                if not fin:
                    continue
                try:
                    m = json.loads(buf.decode('utf-8'))
                except ValueError:
                    m = None
                buf = b''
                if isinstance(m, dict):
                    handle(c, m)
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
        pass
    finally:
        c.alive = False
        conns.discard(c)
        if c.p:
            broadcast(c.room, {'t': 'leave', 'id': c.id})
        wtask.cancel()
        try:
            writer.close()
        except Exception:
            pass


async def serve_static(writer, method, path):
    if path in ('', '/'):
        path = '/index.html'
    full = os.path.normpath(os.path.join(ROOT, path.lstrip('/')))
    ext = os.path.splitext(full)[1].lower()
    if not full.startswith(ROOT + os.sep) or ext not in STATIC_EXT or not os.path.isfile(full):
        body = b'Nao encontrado'
        writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
        return
    with open(full, 'rb') as f:
        body = f.read()
    ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
    if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
        ctype += '; charset=utf-8'
    writer.write((f'HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n'
                  'Cache-Control: no-cache\r\nConnection: close\r\n\r\n').encode())
    if method != 'HEAD':
        writer.write(body)


async def on_client(reader, writer):
    try:
        req = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
        lines = req.decode('latin-1').split('\r\n')
        method, path, _ = lines[0].split(' ', 2)
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                k, v = line.split(':', 1)
                headers[k.strip().lower()] = v.strip()
        path = path.split('?', 1)[0]
        if path == '/ws' and headers.get('upgrade', '').lower() == 'websocket':
            await ws_session(reader, writer, headers)
            return
        if method in ('GET', 'HEAD'):
            await serve_static(writer, method, path)
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def tick_loop():
    n = 0
    while True:
        await asyncio.sleep(TICK)
        n += 1
        rooms = {}
        for c in conns:
            if c.p:
                p = c.p
                rooms.setdefault(c.room, []).append([c.id, round(p['x'], 2), round(p['y'], 2), round(p['z'], 2),
                                                     round(p['h'], 3), p['a'], p['v'], p['pvp'], p['atk']])
        for room, ps in rooms.items():
            msg = {'t': 'snap', 'ps': ps}
            for c in in_room(room):
                c.send(msg)
        if n % 20 == 0:
            msg = counts()
            for c in conns:
                c.send(msg)


async def main():
    server = await asyncio.start_server(on_client, HOST, PORT)
    asyncio.create_task(tick_loop())
    print(f'Avenida Livre rodando em http://localhost:{PORT}  (Ctrl+C para parar)', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

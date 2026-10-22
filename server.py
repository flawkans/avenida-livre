#!/usr/bin/env python3
"""Servidor do Avenida Livre: entrega o jogo e o multiplayer por WebSocket.

Só usa a biblioteca padrão do Python 3.8+. Rode com:  python server.py  [porta]
A variável de ambiente PORT, se existir, tem prioridade (é o que Render/Railway usam). Padrão: 8080.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import random
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 8080))
HOST = os.environ.get('HOST', '0.0.0.0')
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

MAPS = {'paulista', 'copacabana', 'esplanada'}
TEAMS = {'va', 'vm', 'pa'}
VEHICLES = {'pe', 'carro', 'caixa', 'roadster', 'carreta', 'dirigivel', 'moto', 'chopper', 'girafa', 'dino',
            'calango', 'onca', 'pipoca', 'somcar', 'som'}
ANIMS = {'idle', 'walk', 'run', 'push', 'pedal', 'dance', 'wave', 'clap', 'sit', 'sitground', 'ride',
         'stand', 'jump', 'spray', 'ko'}
STAMPS = {'tag', 'coracao', 'estrela', 'paz', 'time', 'smile'}
BAD = ['idiota', 'burro', 'lixo', 'otario', 'otário', 'imbecil', 'merda', 'porra', 'caralho', 'fdp',
       'vagabundo', 'vagabunda']
STATIC_EXT = {'.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.webp', '.svg', '.ico', '.json', '.woff2', '.mp3', '.ogg'}
MAX_FRAME = 1024 * 1024        # 1 MB: cabe a imagem de um anúncio
MAX_PER_ROOM = 300
TICK = 0.1

# Anúncios nos telões (regras do original): lance mínimo por telão, 5 vagas por telão ordenadas pelo valor,
# cada anúncio fica garantido 2 h nas vagas e dura até 20 dias. Link só a partir de R$ 250.
ADS_DIR = os.path.join(ROOT, 'ads')
ADS_FILE = os.path.join(ADS_DIR, 'ads.json')
AD_FLOOR_DEFAULT = 500
AD_FLOORS = {'masp-principal': 1000, 'haddock-lobo': 250, 'bela-cintra': 250}
AD_LINK_MIN = 250
AD_MAX = 10000
AD_SLOTS = 5
AD_PROTECT_S = 2 * 3600
AD_LIFETIME_S = 20 * 24 * 3600
AD_IMG_MAX = 700 * 1024

# Login (Supabase): com SUPABASE_URL e SUPABASE_ANON_KEY definidos, só joga e conversa quem tem conta.
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
AUTH_ON = bool(SUPABASE_URL and SUPABASE_KEY)
_auth_cache = {}   # token -> (validade, user_id, nick, premium)
# Pagamento do Premium (Cakto): a Cakto avisa em POST /api/cakto; o servidor libera o Premium no Supabase.
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')   # chave SECRETA: só no painel do Render, nunca no GitHub
CAKTO_SECRET = os.environ.get('CAKTO_SECRET', '')
CAKTO_PRODUCT_ID = os.environ.get('CAKTO_PRODUCT_ID', '')            # opcional: só aceita avisos deste produto

# Combate: dano, alcance (m), intervalo entre golpes (s) e se é exclusivo do Premium.
WEAPONS = {'soco': (10, 1.9, .45, False), 'bastao': (25, 2.5, .6, True), 'espada': (32, 2.9, .7, True),
           'pistola': (18, 32, .35, True), 'paintball': (14, 32, .3, True)}
MAX_HP = 100
RESPAWN_FREE_S = 300          # sem Premium: espera 5 min para reviver
_deaths = {}                  # user_id -> horário em que pode reviver (não adianta sair e voltar)

conns = set()
_next_id = 1
ads = []


def _supabase_get(path, token):
    req = urllib.request.Request(SUPABASE_URL + path, headers={'apikey': SUPABASE_KEY, 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode('utf-8'))


def _verify_blocking(token):
    user = _supabase_get('/auth/v1/user', token)
    uid = user.get('id')
    if not uid:
        return None
    q = urllib.parse.quote(uid)
    rows = _supabase_get('/rest/v1/profiles?select=nick&id=eq.' + q, token)
    ent = _supabase_get('/rest/v1/entitlements?select=premium&user_id=eq.' + q, token)
    nick = rows[0]['nick'] if rows else None
    return uid, nick, bool(ent and ent[0].get('premium'))


async def verify_token(token, fresh=False):
    """Confere o token do jogador no Supabase. Devolve (user_id, apelido, premium) ou None."""
    if not AUTH_ON or not isinstance(token, str) or len(token) > 4096:
        return None
    now = time.time()
    hit = _auth_cache.get(token)
    if hit and hit[0] > now and not fresh:
        return hit[1], hit[2], hit[3]
    try:
        res = await asyncio.to_thread(_verify_blocking, token)
    except (urllib.error.URLError, ValueError, OSError, KeyError, IndexError):
        res = None
    if res and res[1]:
        if len(_auth_cache) > 5000:
            _auth_cache.clear()
        _auth_cache[token] = (now + 300, res[0], res[1], res[2])
        return res
    return None


def _service_rpc(fn, args):
    req = urllib.request.Request(SUPABASE_URL + '/rest/v1/rpc/' + fn, data=json.dumps(args).encode('utf-8'), method='POST',
                                 headers={'apikey': SUPABASE_SERVICE_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode('utf-8') or 'null')


def cakto_valid(headers, raw, payload):
    """Confere se o aviso veio mesmo da Cakto: assinatura HMAC no cabeçalho (preferida) ou o segredo no corpo."""
    if not CAKTO_SECRET:
        return False
    sig, ts = headers.get('x-cakto-signature', ''), headers.get('x-cakto-timestamp', '')
    if sig.startswith('v1=') and ts:
        try:
            if abs(time.time() - float(ts)) > 300:
                return False
        except ValueError:
            return False
        good = hmac.new(CAKTO_SECRET.encode(), ts.encode() + b'.' + raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig[3:])
    return hmac.compare_digest(str(payload.get('secret', '')), CAKTO_SECRET)


async def handle_cakto(headers, raw):
    try:
        payload = json.loads(raw.decode('utf-8'))
    except ValueError:
        return 400, {'ok': False, 'erro': 'json'}
    if not isinstance(payload, dict) or not cakto_valid(headers, raw, payload):
        return 401, {'ok': False, 'erro': 'segredo'}
    event = payload.get('event', '')
    items = payload.get('data')
    items = items if isinstance(items, list) else [items]
    done = []
    for d in items:
        if not isinstance(d, dict):
            continue
        email = ((d.get('customer') or {}).get('email') or '').strip().lower()
        order = str(d.get('id') or d.get('refId') or '')
        product = str((d.get('product') or {}).get('id') or '')
        if not email or not order or (CAKTO_PRODUCT_ID and product != CAKTO_PRODUCT_ID):
            continue
        try:
            if event == 'purchase_approved':
                r = await asyncio.to_thread(_service_rpc, 'liberar_premium', {'p_email': email, 'p_order': order, 'p_valor': d.get('amount'), 'p_dados': d})
            elif event in ('refund', 'chargeback', 'purchase_refunded', 'purchase_chargeback'):
                r = await asyncio.to_thread(_service_rpc, 'remover_premium', {'p_email': email, 'p_order': order, 'p_evento': event})
            else:
                continue
        except (urllib.error.URLError, ValueError, OSError) as e:
            print('Cakto: falha ao gravar no Supabase:', e, flush=True)
            return 500, {'ok': False}
        print(f'Cakto: {event} {order} -> {r}', flush=True)
        done.append(r)
    return 200, {'ok': True, 'feito': done}


def load_ads():
    global ads
    try:
        with open(ADS_FILE, encoding='utf-8') as f:
            ads = json.load(f)
    except (OSError, ValueError):
        ads = []


def save_ads():
    os.makedirs(ADS_DIR, exist_ok=True)
    tmp = ADS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(ads, f, ensure_ascii=False)
    os.replace(tmp, ADS_FILE)


def active_ads():
    """Até 5 anúncios por telão: os protegidos (primeiras 2 h) entram primeiro, depois os maiores lances."""
    now = time.time()
    live = [a for a in ads if now - a['since'] < AD_LIFETIME_S]
    out = []
    for screen in {a['screen'] for a in live}:
        lst = sorted((a for a in live if a['screen'] == screen), key=lambda a: -a['amount'])
        prot = [a for a in lst if now - a['since'] < AD_PROTECT_S][:AD_SLOTS]
        rest = [a for a in lst if a not in prot][:max(0, AD_SLOTS - len(prot))]
        out += sorted(prot + rest, key=lambda a: -a['amount'])
    return out


def ads_msg():
    return {'t': 'ads', 'ads': [{k: a[k] for k in ('id', 'screen', 'nick', 'amount', 'kind', 'img', 'title', 'sub', 'bg', 'fg', 'link', 'since')} for a in active_ads()]}


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
        self.uid = None         # id da conta (Supabase), quando o login está ligado
        self.auth_nick = None   # apelido da conta: o cliente não consegue trocar por mensagem
        self.premium = False    # vem da tabela entitlements (só a Cakto/servidor grava lá)
        self.hp = MAX_HP
        self.dead_until = 0.0   # >0: morto; pode reviver a partir deste horário
        self.weapon_t = {}
        self.hurt_t = 0.0       # último dano recebido (a vida volta sozinha 8 s depois)

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
            'x': p['x'], 'y': p['y'], 'z': p['z'], 'h': p['h'], 'dead': c.dead_until > 0}


def is_premium(c):
    return c.premium if AUTH_ON else bool(os.environ.get('DEV_PREMIUM'))   # sem login (teste local): DEV_PREMIUM=1 libera


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


FREE_CHARS = {'homem', 'mulher', 'tiozao', 'velho', 'velha', 'jovem', 'jovemf'}
PAID_FLAGS = {'brasilp', 'mito', 'b17', 'mito22', 'f22p', 'euabr', 'ptp', 'lulala', 'lula2002', 'amor', 'brpt', 'prendeu', 'sua'}
PAID_OBJS = {'sorvete', 'w_bastao', 'w_espada', 'w_pistola', 'w_paintball'}


def premium_filter(c, look, held):
    """Sem Premium: personagem especial vira o básico e bandeira/objeto/arma pagos saem da mão."""
    if is_premium(c):
        return look, held
    if look.get('char') not in FREE_CHARS:
        look['char'] = 'homem'
    if held and ((held['type'] == 'flag' and held['id'] in PAID_FLAGS) or (held['type'] == 'obj' and held['id'] in PAID_OBJS)):
        held = None
    return look, held


def apply_profile(c, m):
    old = c.p or {}
    look, held = premium_filter(c, clean_look(m.get('look')), clean_held(m.get('held')))
    m = dict(m, look=look, held=held)
    c.p = {
        'nick': c.auth_nick or clean_text(m.get('nick'), 16) or 'Anônimo',
        'team': m.get('team') if m.get('team') in TEAMS else 'va',
        'look': m['look'],
        'held': m['held'],
        'prem': is_premium(c),
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
        if c.uid and _deaths.get(c.uid, 0) > time.time() and not is_premium(c):   # morreu e saiu: continua morto
            c.dead_until, c.hp = _deaths[c.uid], 0
        if room != c.room:
            if was:
                broadcast(c.room, {'t': 'leave', 'id': c.id}, exclude=c)
            c.room = room
        c.send({'t': 'roster', 'ps': roster(room)})
        c.send({'t': 'me', 'premium': is_premium(c), 'hp': c.hp, 'dead': c.dead_until > 0, 'wait': max(0, round(c.dead_until - time.time()))})
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
        p['a'] = 'ko' if c.dead_until else (m.get('a') if m.get('a') in ANIMS else 'idle')
        p['v'] = m.get('v') if m.get('v') in VEHICLES else 'pe'
        if p['v'] not in ('pe', 'pipoca') and not is_premium(c):   # veículos são do Premium
            p['v'] = 'pe'
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
    elif t == 'super' and c.p:   # superchat é benefício do Premium (sem cobrança à parte)
        if not is_premium(c) or now - c.chat_t < 3:
            return
        c.chat_t = now
        voice = m.get('voice') if m.get('voice') in ('homem', 'mulher', 'tiozao') else None
        text = clean_text(m.get('text'), 200)
        if text:
            broadcast(c.room, {'t': 'super', 'id': c.id, 'nick': c.p['nick'], 'team': c.p['team'], 'text': text,
                               'amount': 0, 'voice': voice, 'prem': True}, exclude=c)
    elif t == 'hit' and c.p:
        handle_hit(c, m, now)
    elif t == 'botHit' and c.p:
        handle_bot_hit(c, m, now)
    elif t == 'shot' and c.p:   # só o efeito visual do tiro para os outros; o dano vem pelo 'hit'
        w = m.get('w')
        if w not in ('pistola', 'paintball') or not is_premium(c) or c.dead_until or now - c.weapon_t.get('shot', 0) < WEAPONS[w][2] * .8:
            return
        c.weapon_t['shot'] = now
        broadcast(c.room, {'t': 'shot', 'id': c.id, 'w': w, 'x': num(m.get('x'), -500, 500), 'y': num(m.get('y'), -5, 120), 'z': num(m.get('z'), -500, 500),
                           'dx': num(m.get('dx'), -1, 1), 'dz': num(m.get('dz'), -1, 1), 'c': m.get('c') if isinstance(m.get('c'), str) and re.fullmatch(r'#[0-9a-fA-F]{6}', m.get('c')) else '#ff3b6b'}, exclude=c)
    elif t == 'respawn' and c.p:
        if not c.dead_until:
            return
        if is_premium(c) or time.time() >= c.dead_until:
            c.dead_until, c.hp = 0.0, MAX_HP
            _deaths.pop(c.uid, None)
            c.send({'t': 'respawned', 'hp': c.hp})
            broadcast(c.room, {'t': 'alive', 'id': c.id}, exclude=c)
        else:
            c.send({'t': 'dead', 'wait': round(c.dead_until - time.time()), 'premium': False})
    elif t == 'ad':
        handle_ad(c, m)
    elif t == 'report':
        line = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'by': c.p['nick'] if c.p else None,
                'target': clean_text(m.get('nick'), 16), 'text': clean_text(m.get('text'), 160)}
        with open(os.path.join(ROOT, 'denuncias.log'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')


def handle_hit(c, m, now):
    """Golpe ou tiro de um jogador em outro. O servidor confere tudo e é quem tira a vida."""
    w = m.get('w') if m.get('w') in WEAPONS else 'soco'
    dmg, reach, cooldown, prem_only = WEAPONS[w]
    if c.dead_until or (prem_only and not is_premium(c)) or now - c.weapon_t.get(w, 0) < cooldown * .8:
        return
    target = next((o for o in conns if o.id == m.get('id') and o.p and o.room == c.room), None)
    if not target or target is c or target.dead_until or target.p['team'] == c.p['team']:
        return   # só times diferentes se machucam
    if (target.p['x'] - c.p['x']) ** 2 + (target.p['z'] - c.p['z']) ** 2 > (reach + 1.5) ** 2:
        return
    c.weapon_t[w] = now
    if hurt(target, dmg, w, c.p['nick'], now, c.id):
        c.send({'t': 'kill', 'nick': target.p['nick']})


def handle_bot_hit(c, m, now):
    """Os bots rodam no navegador de cada jogador, então quem avisa do golpe é o próprio atingido.
    Ele só consegue se machucar (nunca outra pessoa), e no máximo no ritmo de uma arma Premium."""
    w = m.get('w') if m.get('w') in WEAPONS else 'soco'
    if c.dead_until or now - c.weapon_t.get('bot', 0) < .3:
        return
    c.weapon_t['bot'] = now
    hurt(c, WEAPONS[w][0], w, clean_text(m.get('by'), 16) or 'um bot', now)


def hurt(target, dmg, w, by, now, from_id=None):
    """Tira vida de um jogador; se zerar, ele cai (sem Premium só revive depois de 5 minutos)."""
    target.hurt_t = now
    target.hp = max(0, target.hp - dmg)
    target.send({'t': 'dmg', 'from': from_id, 'nick': by, 'amount': dmg, 'hp': target.hp, 'w': w})
    if target.hp > 0:
        return False
    wait = 0 if is_premium(target) else RESPAWN_FREE_S
    target.dead_until = time.time() + wait if wait else time.time()
    if target.uid and wait:
        _deaths[target.uid] = target.dead_until
    target.p['a'] = 'ko'
    target.send({'t': 'dead', 'by': by, 'wait': wait, 'premium': is_premium(target)})
    broadcast(target.room, {'t': 'died', 'id': target.id, 'by': by, 'nick': target.p['nick'], 'w': w})
    return True


def handle_ad(c, m):
    if AUTH_ON and not c.uid:
        return c.send({'t': 'adFail', 'msg': 'Entre na sua conta para anunciar.'})
    screen = m.get('screen')
    if not isinstance(screen, str) or not re.fullmatch(r'[a-z0-9-]{1,40}', screen):
        return c.send({'t': 'adFail', 'msg': 'Telão inválido.'})
    floor = AD_FLOORS.get(screen, AD_FLOOR_DEFAULT)
    amount = int(num(m.get('amount'), 0, AD_MAX))
    if amount < floor:
        return c.send({'t': 'adFail', 'msg': f'O lance mínimo deste telão é R$ {floor}.'})
    nick = (c.p or {}).get('nick') or clean_text(m.get('nick'), 16) or 'Anunciante'
    link = clean_text(m.get('link'), 300)
    if link and (amount < AD_LINK_MIN or not re.fullmatch(r'https?://[^\s@]+\.[^\s@]+', link)):
        link = ''
    hexc = lambda v, d: v if isinstance(v, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', v) else d
    ad = {'id': base64.urlsafe_b64encode(os.urandom(9)).decode(), 'screen': screen, 'nick': nick, 'amount': amount,
          'kind': 'text', 'img': None, 'title': clean_text(m.get('title'), 40), 'sub': clean_text(m.get('sub'), 60),
          'bg': hexc(m.get('bg'), '#ecd27e'), 'fg': hexc(m.get('fg'), '#14201a'), 'link': link, 'since': time.time()}
    img = m.get('img')
    if isinstance(img, str) and img.startswith('data:image/jpeg;base64,'):
        try:
            raw = base64.b64decode(img.split(',', 1)[1], validate=True)
        except ValueError:
            raw = b''
        if not raw.startswith(b'\xff\xd8') or len(raw) > AD_IMG_MAX:
            return c.send({'t': 'adFail', 'msg': 'Imagem inválida ou grande demais.'})
        os.makedirs(ADS_DIR, exist_ok=True)
        name = hashlib.sha1(raw).hexdigest()[:24] + '.jpg'
        with open(os.path.join(ADS_DIR, name), 'wb') as f:
            f.write(raw)
        ad['kind'], ad['img'] = 'image', 'ads/' + name
    elif not ad['title']:
        return c.send({'t': 'adFail', 'msg': 'Envie uma imagem ou escreva um título.'})
    ads.append(ad)
    save_ads()
    c.send({'t': 'adOk', 'id': ad['id']})
    msg = ads_msg()
    for o in conns:
        o.send(msg)


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
    c.send(ads_msg())
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
                    if m.get('t') == 'hello' and AUTH_ON:
                        auth = await verify_token(m.get('token'), bool(m.get('fresh')))
                        if not auth:
                            c.send({'t': 'needLogin', 'msg': 'Entre na sua conta para jogar e conversar. Sem conta, você pode assistir do alto.'})
                            continue
                        c.uid, c.auth_nick, c.premium = auth
                        for o in list(conns):   # a mesma conta em outra aba/aparelho sai da avenida
                            if o is not c and o.uid == c.uid and o.p:
                                broadcast(o.room, {'t': 'leave', 'id': o.id}, exclude=o)
                                o.p = None
                                o.send({'t': 'kicked', 'msg': 'Você entrou com esta conta em outro lugar.'})
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
        if path == '/api/cakto' and method == 'POST':   # aviso de pagamento da Cakto
            length = int(headers.get('content-length', '0') or 0)
            raw = await reader.readexactly(length) if 0 < length <= 1024 * 1024 else b''
            status, out = await handle_cakto(headers, raw)
            body = json.dumps(out).encode()
            writer.write(b'HTTP/1.1 %d OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (status, len(body)) + body)
            await writer.drain()
            return
        if path == '/api/status':   # diagnóstico: o login está ligado neste servidor?
            body = json.dumps({'login': AUTH_ON, 'supabase': SUPABASE_URL or None, 'players': sum(1 for c in conns if c.p),
                               'pagamento': bool(CAKTO_SECRET and SUPABASE_SERVICE_KEY)}).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain()
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
        mono = time.monotonic()
        for c in conns:
            if c.p and not c.dead_until and c.hp < MAX_HP and mono - c.hurt_t > 8:   # mesmo ritmo do jogo: 4 de vida por segundo
                c.hp = min(MAX_HP, c.hp + 4 * TICK)
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
        if n % 600 == 0:  # a cada minuto, tira os anúncios vencidos dos telões
            msg = ads_msg()
            for c in conns:
                c.send(msg)


async def main():
    load_ads()
    server = await asyncio.start_server(on_client, HOST, PORT)
    asyncio.create_task(tick_loop())
    print(f'Avenida Livre rodando em http://localhost:{PORT}  (Ctrl+C para parar)', flush=True)
    print('Login (Supabase): ' + ('LIGADO · ' + SUPABASE_URL if AUTH_ON else 'desligado (faltam SUPABASE_URL e SUPABASE_ANON_KEY)'), flush=True)
    print('Pagamento (Cakto): ' + ('LIGADO' if CAKTO_SECRET and SUPABASE_SERVICE_KEY else 'desligado (faltam CAKTO_SECRET e SUPABASE_SERVICE_KEY)'), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
